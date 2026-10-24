#ifdef _WIN32
// DUCKDB_STATIC_BUILD removes the default Windows extension export decoration.
#define DUCKDB_EXTENSION_API __declspec(dllexport)
#endif
#include "duckdb.hpp"
#include "duckdb/main/config.hpp"
#include "duckdb/main/extension/extension_loader.hpp"
#include "duckdb/planner/filter/constant_filter.hpp"
#include "duckdb/planner/filter/conjunction_filter.hpp"
#include "duckdb/planner/filter/in_filter.hpp"

#include <algorithm>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <random>
#include <set>
#include <sstream>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace fileindex {
using namespace duckdb;
namespace fs = std::filesystem;
constexpr uint64_t MAX_RECORD = 64ULL * 1024 * 1024;
constexpr uint64_t MAX_TEXT = 16ULL * 1024 * 1024;
constexpr uint64_t MAX_COLUMNS = 4096;
constexpr uint64_t MAX_KEYS = 32;

static void Check(bool ok, const string &message) {
    if (!ok) throw IOException("fileindex: " + message);
}
static void Allowed(ClientContext &context) {
    Check(DBConfig::GetConfig(context).options.enable_external_access,
          "external access is disabled");
    if (context.interrupted.load()) throw InterruptException();
}
static string PathText(const fs::path &path) { return path.generic_u8string(); }
static fs::path Path(const string &text) { return fs::u8path(text); }
static uint64_t Tell(std::istream &s) {
    auto p = s.tellg();
    Check(p >= 0, "cannot determine file position");
    return static_cast<uint64_t>(p);
}
static uint64_t Tell(std::ostream &s) {
    auto p = s.tellp();
    Check(p >= 0, "cannot determine output position");
    return static_cast<uint64_t>(p);
}
static void Put(std::ostream &s, uint64_t n) {
    for (unsigned i = 0; i < 8; ++i) s.put(static_cast<char>((n >> (8 * i)) & 255));
    Check(bool(s), "write failed (check disk space)");
}
static uint64_t Get(std::istream &s) {
    uint64_t n = 0;
    for (unsigned i = 0; i < 8; ++i) {
        int c = s.get();
        Check(c != EOF, "truncated index");
        n |= uint64_t(static_cast<unsigned char>(c)) << (8 * i);
    }
    return n;
}
static void PutText(std::ostream &s, const string &text) {
    Check(text.size() <= MAX_TEXT, "text exceeds 16 MiB");
    Put(s, text.size());
    s.write(text.data(), static_cast<std::streamsize>(text.size()));
    Check(bool(s), "write failed");
}
static string GetText(std::istream &s) {
    auto n = Get(s);
    Check(n <= MAX_TEXT, "invalid index text length");
    string text(static_cast<size_t>(n), '\0');
    s.read(&text[0], static_cast<std::streamsize>(n));
    Check(bool(s), "truncated index text");
    return text;
}
static uint64_t Modified(const string &path) {
    return static_cast<uint64_t>(fs::last_write_time(Path(path)).time_since_epoch().count());
}
static uint64_t Fingerprint(const string &path, ClientContext &context) {
    std::ifstream in(Path(path), std::ios::binary);
    Check(bool(in), "cannot open source");
    uint64_t h = 14695981039346656037ULL;
    char buffer[65536];
    while (in) {
        Allowed(context);
        in.read(buffer, sizeof(buffer));
        for (std::streamsize i = 0; i < in.gcount(); ++i) {
            h ^= static_cast<unsigned char>(buffer[i]);
            h *= 1099511628211ULL;
        }
    }
    Check(in.eof(), "source read failed");
    return h;
}
static void UTF8(const string &s) {
    for (size_t i = 0; i < s.size();) {
        auto c = static_cast<unsigned char>(s[i++]);
        if (c < 128) continue;
        unsigned n = c >= 0xC2 && c <= 0xDF ? 1 : c >= 0xE0 && c <= 0xEF ? 2 :
                     c >= 0xF0 && c <= 0xF4 ? 3 : 99;
        Check(n != 99 && i + n <= s.size(), "invalid UTF-8; transcode the source first");
        auto first = static_cast<unsigned char>(s[i]);
        Check(!(c == 0xE0 && first < 0xA0) && !(c == 0xED && first >= 0xA0) &&
              !(c == 0xF0 && first < 0x90) && !(c == 0xF4 && first >= 0x90), "invalid UTF-8");
        while (n--) Check((static_cast<unsigned char>(s[i++]) & 0xC0) == 0x80, "invalid UTF-8");
    }
}
static int64_t Integer(const string &s) {
    int64_t result = 0;
    auto converted = std::from_chars(s.data(), s.data() + s.size(), result);
    Check(converted.ec == std::errc() && converted.ptr == s.data() + s.size(),
          "BIGINT key must be an integer in signed 64-bit range");
    return result;
}

struct KeyColumn { uint64_t column; bool numeric; };
struct Header {
    string source, format = "csv", null_text = "\\N";
    uint64_t size = 0, modified = 0, fingerprint = 0, rows = 0, directory = 0;
    uint64_t record_length = 0, version = 1;
    bool header = true;
    vector<KeyColumn> keys = {{0, false}};
    char delimiter = ',';
    vector<uint64_t> widths;
    vector<string> names;
};
static void WriteHeader(std::ostream &out, const Header &h) {
    out.write(h.version == 1 ? "YYFIDX01" : "YYFIDX02", 8);
    PutText(out, h.source); PutText(out, h.format); PutText(out, h.null_text);
    Put(out, h.size); Put(out, h.modified); Put(out, h.fingerprint);
    Put(out, h.rows); Put(out, h.directory); Put(out, h.record_length);
    if (h.version == 1) {
        Put(out, h.keys[0].column); Put(out, h.header); Put(out, h.keys[0].numeric);
    } else {
        Put(out, h.keys.size());
        for (auto &key : h.keys) { Put(out, key.column); Put(out, key.numeric); }
        Put(out, h.header);
    }
    Put(out, static_cast<unsigned char>(h.delimiter));
    Put(out, h.widths.size());
    for (auto n : h.widths) Put(out, n);
    Put(out, h.names.size());
    for (auto &n : h.names) PutText(out, n);
}
static Header ReadHeader(std::istream &in, uint64_t index_size) {
    char magic[8]; in.read(magic, 8);
    Check(bool(in) && (string(magic, 8) == "YYFIDX01" || string(magic, 8) == "YYFIDX02"), "not a supported fileindex index");
    Header h;
    h.version = magic[7] == '1' ? 1 : 2;
    h.source = GetText(in); h.format = GetText(in); h.null_text = GetText(in);
    h.size = Get(in); h.modified = Get(in); h.fingerprint = Get(in);
    h.rows = Get(in); h.directory = Get(in); h.record_length = Get(in);
    uint64_t header;
    if (h.version == 1) {
        auto column = Get(in); header = Get(in); auto numeric = Get(in);
        Check(numeric <= 1, "invalid key type"); h.keys = {{column, numeric != 0}};
    } else {
        auto key_count = Get(in); Check(key_count > 0 && key_count <= MAX_KEYS, "invalid key count");
        h.keys.clear();
        for (uint64_t i = 0; i < key_count; ++i) {
            auto column = Get(in), numeric = Get(in); Check(numeric <= 1, "invalid key type");
            h.keys.push_back({column, numeric != 0});
        }
        header = Get(in);
    }
    auto delimiter = Get(in);
    Check(header <= 1 && delimiter > 0 && delimiter < 128, "invalid index options");
    h.header = header != 0; h.delimiter = static_cast<char>(delimiter);
    auto count = Get(in); Check(count <= MAX_COLUMNS, "invalid width count");
    uint64_t total = 0;
    for (uint64_t i = 0; i < count; ++i) {
        auto width = Get(in); Check(width > 0 && width <= MAX_RECORD && total <= MAX_RECORD - width, "invalid widths");
        h.widths.push_back(width); total += width;
    }
    count = Get(in); Check(count > 0 && count <= MAX_COLUMNS, "invalid column count");
    std::set<string> seen;
    for (uint64_t i = 0; i < count; ++i) {
        auto name = GetText(in); UTF8(name);
        Check(!name.empty() && seen.insert(StringUtil::Lower(name)).second, "invalid index column name");
        h.names.push_back(std::move(name));
    }
    Check(h.format == "csv" || h.format == "fixed", "invalid schema");
    std::set<uint64_t> key_columns;
    for (auto &key : h.keys) Check(key.column < count && key_columns.insert(key.column).second, "invalid/duplicate key column");
    if (h.format == "fixed") Check(!h.header && total == h.record_length && count == h.widths.size() &&
                                   h.record_length > 0 && h.record_length <= MAX_RECORD, "invalid fixed schema");
    Check(h.directory >= Tell(in) && h.directory <= index_size &&
          h.rows <= (index_size - h.directory) / 8 && h.directory + h.rows * 8 == index_size,
          "invalid index directory");
    return h;
}
static void Validate(const Header &h, ClientContext &context, bool verify) {
    Allowed(context);
    Check(fs::file_size(Path(h.source)) == h.size && Modified(h.source) == h.modified,
          "source changed; rebuild the index with file_index_create");
    if (verify) Check(Fingerprint(h.source, context) == h.fingerprint,
                      "source fingerprint changed; rebuild the index");
}

// Byte offsets are physical positions, including quoted CSV newlines and CRLF.
struct Reader {
    std::ifstream in;
    const Header &h;
    uint64_t position = 0;
    explicit Reader(const Header &header) : in(Path(header.source), std::ios::binary), h(header) {
        Check(bool(in), "cannot open source");
        if (h.format == "csv") {
            char bom[3] = {}; in.read(bom, 3); in.clear();
            if (string(bom, 3) == "\xEF\xBB\xBF") position = 3;
            in.seekg(static_cast<std::streamoff>(position));
        }
    }
    int Take() { auto c = in.get(); if (c != EOF) ++position; return c; }
    bool Next(vector<string> &fields, uint64_t &offset, uint64_t &length) {
        fields.clear(); offset = position;
        if (in.peek() == EOF) { Check(!in.bad(), "source read failed"); return false; }
        if (h.format == "fixed") {
            string record(static_cast<size_t>(h.record_length), '\0');
            in.read(&record[0], static_cast<std::streamsize>(h.record_length));
            Check(static_cast<uint64_t>(in.gcount()) == h.record_length, "incomplete fixed-length record");
            position += h.record_length;
            size_t start = 0;
            for (auto width : h.widths) {
                fields.push_back(record.substr(start, static_cast<size_t>(width)));
                start += static_cast<size_t>(width);
            }
        } else {
            string field;
            bool quoted = false, closed = false, start = true;
            while (true) {
                Check(position - offset <= MAX_RECORD && field.size() <= MAX_TEXT && fields.size() < MAX_COLUMNS,
                      "record/field/column limit exceeded");
                auto c = Take();
                if (quoted) {
                    Check(c != EOF, "unterminated quoted CSV field");
                    if (c == '"') {
                        if (in.peek() == '"') { Take(); field += '"'; }
                        else { quoted = false; closed = true; }
                    } else field += static_cast<char>(c);
                } else if (c == h.delimiter || c == '\r' || c == '\n' || c == EOF) {
                    fields.push_back(std::move(field)); field.clear(); start = true; closed = false;
                    if (c == '\r') { Check(Take() == '\n', "CSV CR must be followed by LF"); break; }
                    if (c == '\n' || c == EOF) break;
                } else {
                    Check(!closed, "unexpected text after CSV closing quote");
                    if (c == '"') { Check(start, "quote inside unquoted CSV field"); quoted = true; }
                    else field += static_cast<char>(c);
                    start = false;
                }
            }
        }
        length = position - offset;
        Check(length <= MAX_RECORD, "record exceeds 64 MiB");
        for (auto &field : fields) { Check(field.size() <= MAX_TEXT, "field exceeds 16 MiB"); UTF8(field); }
        return true;
    }
    vector<string> At(uint64_t offset, uint64_t length) {
        Check(offset <= h.size && length <= h.size - offset && length > 0 && length <= MAX_RECORD,
              "index points outside source");
        in.clear(); in.seekg(static_cast<std::streamoff>(offset)); position = offset;
        vector<string> fields; uint64_t actual_offset, actual_length;
        Check(Next(fields, actual_offset, actual_length) && actual_length == length, "invalid record position");
        return fields;
    }
};

struct Key {
    bool null = false;
    string text;
    int64_t number = 0;
};
static Key MakeKey(const string &s, const Header &h, size_t component) {
    Key k; k.null = s == h.null_text;
    if (!k.null) { if (h.keys[component].numeric) k.number = Integer(s); else k.text = s; }
    return k;
}
static int Compare(const Key &a, const Key &b, bool numeric) {
    if (a.null || b.null) return a.null == b.null ? 0 : a.null ? -1 : 1;
    if (numeric) return a.number < b.number ? -1 : a.number > b.number ? 1 : 0;
    int c = a.text.compare(b.text); return c < 0 ? -1 : c > 0 ? 1 : 0;
}
static bool NumericColumn(const Header &h, uint64_t column) {
    for (auto &key : h.keys) if (key.column == column) return key.numeric;
    return false;
}
struct Entry { vector<Key> keys; uint64_t offset, length; };
static void WriteEntry(std::ostream &out, const Entry &e, const Header &h) {
    for (size_t i = 0; i < e.keys.size(); ++i) {
        auto &key = e.keys[i]; Put(out, key.null);
        if (!key.null) { if (h.keys[i].numeric) Put(out, static_cast<uint64_t>(key.number)); else PutText(out, key.text); }
    }
    Put(out, e.offset); Put(out, e.length);
}
struct Index {
    std::ifstream in;
    Header h;
    uint64_t start;
    explicit Index(const string &path) : in(Path(path), std::ios::binary) {
        Check(bool(in), "cannot open index: " + path);
        h = ReadHeader(in, fs::file_size(Path(path))); start = Tell(in);
    }
    Entry At(uint64_t row) {
        Check(row < h.rows, "invalid index row");
        in.clear(); in.seekg(static_cast<std::streamoff>(h.directory + row * 8));
        auto pos = Get(in); Check(pos >= start && pos < h.directory, "invalid index entry position");
        in.seekg(static_cast<std::streamoff>(pos));
        Entry e; uint64_t key_bytes = 0;
        for (auto &column : h.keys) {
            Key key; auto null = Get(in); Check(null <= 1, "invalid null flag"); key.null = null != 0;
            if (!key.null) {
                if (column.numeric) {
                    auto u = Get(in);
                    key.number = u <= uint64_t(INT64_MAX) ? static_cast<int64_t>(u) : -1 - static_cast<int64_t>(~u);
                } else { key.text = GetText(in); key_bytes += key.text.size(); Check(key_bytes <= MAX_RECORD, "index key exceeds record limit"); }
            }
            e.keys.push_back(std::move(key));
        }
        e.offset = Get(in); e.length = Get(in);
        Check(Tell(in) <= h.directory, "entry overlaps directory");
        return e;
    }
    uint64_t Bound(const vector<Key> &prefix, bool upper) {
        uint64_t low = 0, high = h.rows;
        while (low < high) {
            auto mid = low + (high - low) / 2;
            auto entry = At(mid); int c = 0;
            for (size_t i = 0; i < prefix.size(); ++i) {
                c = Compare(entry.keys[i], prefix[i], h.keys[i].numeric); if (c != 0) break;
            }
            if (c < 0 || (upper && c == 0)) low = mid + 1; else high = mid;
        }
        return low;
    }
};

struct Workspace {
    fs::path path;
    explicit Workspace(const fs::path &parent) {
        std::random_device random;
        for (int i = 0; i < 100; ++i) {
            path = parent / (".fileindex-" + std::to_string(random()) + "-" + std::to_string(random()));
            if (fs::create_directory(path)) return;
        }
        throw IOException("fileindex: cannot create private build directory");
    }
    ~Workspace() { std::error_code error; fs::remove_all(path, error); }
};
static void Query(Connection &connection, const string &sql) {
    auto result = connection.Query(sql); if (result->HasError()) result->ThrowError();
}
static void Names(Header &h, vector<string> &fields) {
    Check(!fields.empty() && fields.size() <= MAX_COLUMNS, "source has no columns");
    if (h.header) h.names = fields;
    else for (size_t i = 0; i < fields.size(); ++i) h.names.push_back("c" + std::to_string(i + 1));
    std::set<string> seen;
    for (auto &n : h.names) Check(!n.empty() && seen.insert(StringUtil::Lower(n)).second, "empty/duplicate column name");
    for (auto &key : h.keys) Check(key.column < h.names.size(), "key_column is outside source schema");
}

struct CreateData : public TableFunctionData {
    Header h;
    string index, memory = "64MB", temp_limit = "4GB";
    bool replace = false;
    unique_ptr<FunctionData> Copy() const override { return make_uniq<CreateData>(*this); }
    bool Equals(const FunctionData &other) const override { return this == &other; }
};
static unique_ptr<FunctionData> BindCreate(ClientContext &context, TableFunctionBindInput &input,
                                         vector<LogicalType> &types, vector<string> &names) {
    Allowed(context);
    auto data = make_uniq<CreateData>();
    for (auto &v : input.inputs) Check(!v.IsNull(), "paths cannot be NULL");
    data->h.source = PathText(fs::canonical(Path(input.inputs[0].GetValue<string>())));
    auto target = fs::absolute(Path(input.inputs[1].GetValue<string>()));
    Check(!target.filename().empty() && fs::is_directory(target.parent_path()), "index parent directory does not exist");
    data->index = PathText(fs::weakly_canonical(target));
    Check(data->h.source != data->index && (!fs::exists(target) || !fs::equivalent(Path(data->h.source), target)),
          "index and source must be different files");
    auto &h = data->h;
    auto has = [&](const string &name) { return input.named_parameters.find(name) != input.named_parameters.end(); };
    Check(!(has("key_columns") || has("key_types")) || !(has("key_column") || has("key_type")),
          "do not mix key_columns/key_types with key_column/key_type");
    Check(!has("key_types") || has("key_columns"), "key_types requires key_columns");
    for (auto &option : input.named_parameters) {
        Check(!option.second.IsNull(), "options cannot be NULL");
        auto &n = option.first; auto &v = option.second;
        if (n == "format") h.format = StringUtil::Lower(v.GetValue<string>());
        else if (n == "header") h.header = v.GetValue<bool>();
        else if (n == "key_column") { auto k = v.GetValue<int64_t>(); Check(k > 0, "key_column is 1-based"); h.keys[0].column = uint64_t(k - 1); }
        else if (n == "key_type") { auto type = StringUtil::Upper(v.GetValue<string>()); Check(type == "VARCHAR" || type == "BIGINT", "key_type must be VARCHAR or BIGINT"); h.keys[0].numeric = type == "BIGINT"; }
        else if (n == "delimiter") { auto s = v.GetValue<string>(); Check(s.size() == 1 && s[0] > 0 && s[0] < 127 && s[0] != '"' && s[0] != '\r' && s[0] != '\n', "delimiter must be one ASCII character"); h.delimiter = s[0]; }
        else if (n == "nullstr") h.null_text = v.GetValue<string>();
        else if (n == "record_length") { auto length = v.GetValue<int64_t>(); Check(length > 0 && uint64_t(length) <= MAX_RECORD, "invalid record_length"); h.record_length = uint64_t(length); }
        else if (n == "widths") {
            for (auto &w : ListValue::GetChildren(v)) { Check(!w.IsNull(), "NULL width"); auto width = w.GetValue<int64_t>(); Check(width > 0 && uint64_t(width) <= MAX_RECORD, "invalid width"); h.widths.push_back(uint64_t(width)); }
        } else if (n == "replace") data->replace = v.GetValue<bool>();
        else if (n == "memory_limit") data->memory = v.GetValue<string>();
        else if (n == "temp_limit") data->temp_limit = v.GetValue<string>();
    }
    if (has("key_columns")) {
        auto &columns = ListValue::GetChildren(input.named_parameters.at("key_columns"));
        Check(!columns.empty() && columns.size() <= MAX_KEYS, "key_columns must contain 1 to 32 columns");
        h.keys.clear(); std::set<uint64_t> seen;
        for (auto &column : columns) {
            Check(!column.IsNull(), "NULL key column"); auto n = column.GetValue<int64_t>();
            Check(n > 0 && uint64_t(n) <= MAX_COLUMNS && seen.insert(uint64_t(n - 1)).second, "invalid/duplicate key column");
            h.keys.push_back({uint64_t(n - 1), false});
        }
        if (has("key_types")) {
            auto &key_types = ListValue::GetChildren(input.named_parameters.at("key_types"));
            Check(key_types.size() == h.keys.size(), "key_types must match key_columns length");
            for (size_t i = 0; i < key_types.size(); ++i) {
                Check(!key_types[i].IsNull(), "NULL key type"); auto type = StringUtil::Upper(key_types[i].GetValue<string>());
                Check(type == "VARCHAR" || type == "BIGINT", "key_type must be VARCHAR or BIGINT");
                h.keys[i].numeric = type == "BIGINT";
            }
        }
        if (h.keys.size() > 1) h.version = 2;
    }
    Check(h.format == "csv" || h.format == "fixed", "format must be csv or fixed");
    if (h.format == "fixed") {
        Check(!h.widths.empty() && h.widths.size() <= MAX_COLUMNS, "fixed format requires widths");
        uint64_t total = 0; for (auto w : h.widths) { Check(total <= MAX_RECORD - w, "record too long"); total += w; }
        if (!h.record_length) h.record_length = total;
        Check(total == h.record_length, "widths must sum to record_length");
        Check(input.named_parameters.find("header") == input.named_parameters.end() || !h.header, "fixed format has no header");
        h.header = false;
    } else Check(h.widths.empty() && h.record_length == 0, "widths/record_length require fixed format");
    Check(DBConfig::ParseMemoryLimit(data->memory) >= 16 * 1024 * 1024, "memory_limit must be at least 16MB");
    Check(DBConfig::ParseMemoryLimit(data->temp_limit) > 0, "temp_limit must be positive");
    types = {LogicalType::VARCHAR, LogicalType::UBIGINT}; names = {"index_path", "rows"};
    return data;
}
struct Once : public GlobalTableFunctionState { bool done = false; };
static unique_ptr<GlobalTableFunctionState> InitOnce(ClientContext &, TableFunctionInitInput &) { return make_uniq<Once>(); }
static void Create(ClientContext &context, TableFunctionInput &input, DataChunk &output) {
    auto &once = input.global_state->Cast<Once>(); if (once.done) return; once.done = true;
    Allowed(context);
    auto &data = input.bind_data->Cast<CreateData>(); auto h = data.h;
    auto target = Path(data.index);
    Check(data.replace || !fs::exists(target), "index already exists; use replace := true to rebuild");
    Workspace workspace(target.parent_path());
    h.size = fs::file_size(Path(h.source)); h.modified = Modified(h.source);
    h.fingerprint = Fingerprint(h.source, context);
    // DuckDB's sorter supplies bounded-memory external sorting.
    DBConfig config;
    config.SetOptionByName("memory_limit", Value(data.memory));
    config.SetOptionByName("threads", Value::BIGINT(2));
    config.SetOptionByName("temp_directory", Value(PathText(workspace.path / "sort")));
    config.SetOptionByName("max_temp_directory_size", Value(data.temp_limit));
    DuckDB sorting(nullptr, &config); Connection connection(sorting);
    string key_names, order, definition;
    for (size_t i = 0; i < h.keys.size(); ++i) {
        auto name = "k" + std::to_string(i);
        if (i) { key_names += ','; order += ','; definition += ','; }
        key_names += name; order += name + " NULLS FIRST";
        definition += name + (h.keys[i].numeric ? " BIGINT" : " VARCHAR");
    }
    Query(connection, "CREATE TABLE entries(" + definition + ",p UBIGINT,n UBIGINT)");
    Reader reader(h); vector<string> fields; uint64_t offset, length;
    bool has_row = reader.Next(fields, offset, length);
    if (h.format == "fixed" && !has_row) fields.resize(h.widths.size());
    Names(h, fields);
    Appender appender(connection, "entries");
    auto append = [&]() {
        Check(fields.size() == h.names.size(), "CSV column count does not match schema");
        appender.BeginRow();
        for (size_t i = 0; i < h.keys.size(); ++i) {
            auto key = MakeKey(fields[h.keys[i].column], h, i);
            if (key.null) appender.Append(nullptr);
            else if (h.keys[i].numeric) appender.Append(key.number);
            else appender.Append(key.text.data(), static_cast<uint32_t>(key.text.size()));
        }
        appender.Append(offset); appender.Append(length); appender.EndRow(); ++h.rows;
    };
    if (has_row && !h.header) append();
    while (reader.Next(fields, offset, length)) { Allowed(context); append(); }
    appender.Close();
    Validate(h, context, true);
    auto sorted = connection.SendQuery("SELECT " + key_names + ",p,n FROM entries ORDER BY " + order + ",p");
    if (sorted->HasError()) sorted->ThrowError();
    auto stage = workspace.path / "index.tmp", directory = workspace.path / "directory.tmp";
    std::ofstream out(stage, std::ios::binary), offsets(directory, std::ios::binary);
    Check(bool(out) && bool(offsets), "cannot create index staging files");
    WriteHeader(out, h); uint64_t written = 0;
    while (auto chunk = sorted->Fetch()) {
        Allowed(context);
        for (idx_t i = 0; i < chunk->size(); ++i) {
            Entry entry;
            for (size_t k = 0; k < h.keys.size(); ++k) {
                Key key; auto v = chunk->GetValue(k, i); key.null = v.IsNull();
                if (!key.null) { if (h.keys[k].numeric) key.number = v.GetValue<int64_t>(); else key.text = v.GetValue<string>(); }
                entry.keys.push_back(std::move(key));
            }
            entry.offset = chunk->GetValue(h.keys.size(), i).GetValue<uint64_t>();
            entry.length = chunk->GetValue(h.keys.size() + 1, i).GetValue<uint64_t>();
            Put(offsets, Tell(out)); WriteEntry(out, entry, h); ++written;
        }
    }
    Check(written == h.rows, "sort row count mismatch");
    offsets.close(); Check(bool(offsets), "directory write failed");
    h.directory = Tell(out);
    std::ifstream dirs(directory, std::ios::binary); char buffer[65536];
    while (dirs) { dirs.read(buffer, sizeof(buffer)); out.write(buffer, dirs.gcount()); }
    Check(dirs.eof(), "directory read failed");
    out.seekp(0); WriteHeader(out, h); out.flush(); Check(bool(out), "index flush failed"); out.close();
    Validate(h, context, true);
    // Publish without a check-then-replace race. Staging is on the same filesystem.
#ifdef _WIN32
    auto published = MoveFileExW(stage.c_str(), target.c_str(), MOVEFILE_WRITE_THROUGH |
                                (data.replace ? MOVEFILE_REPLACE_EXISTING : 0));
    auto error = GetLastError();
    Check(published != 0, "cannot publish index; existing index is unchanged (Windows error " + std::to_string(error) + ")");
#else
    if (!data.replace) {
        fs::create_hard_link(stage, target);
    } else {
        fs::rename(stage, target);
    }
#endif
    output.SetValue(0, 0, Value(data.index)); output.SetValue(1, 0, Value::UBIGINT(h.rows)); output.SetCardinality(1);
}

struct Bound { ExpressionType type; Key key; };
struct ScanData : public TableFunctionData {
    string index;
    Header h;
    bool verify = false;
    unique_ptr<FunctionData> Copy() const override { return make_uniq<ScanData>(*this); }
    bool Equals(const FunctionData &other) const override { return this == &other; }
};
static unique_ptr<FunctionData> BindScan(ClientContext &context, TableFunctionBindInput &input,
                                       vector<LogicalType> &types, vector<string> &names) {
    Allowed(context); Check(!input.inputs[0].IsNull(), "index path cannot be NULL");
    auto data = make_uniq<ScanData>(); data->index = input.inputs[0].GetValue<string>();
    Index index(data->index); data->h = index.h;
    auto verify = input.named_parameters.find("verify");
    if (verify != input.named_parameters.end()) { Check(!verify->second.IsNull(), "verify cannot be NULL"); data->verify = verify->second.GetValue<bool>(); }
    Validate(data->h, context, data->verify);
    names = data->h.names;
    for (idx_t i = 0; i < names.size(); ++i) types.push_back(NumericColumn(data->h, i) ? LogicalType::BIGINT : LogicalType::VARCHAR);
    return data;
}
static Key Constant(const Value &v, bool numeric) {
    Key key; key.null = v.IsNull();
    if (!key.null) { if (numeric) key.number = v.GetValue<int64_t>(); else key.text = v.GetValue<string>(); }
    return key;
}
static bool Matches(const TableFilter &filter, const Key &key, bool numeric) {
    switch (filter.filter_type) {
    case TableFilterType::CONSTANT_COMPARISON: {
        auto &c = filter.Cast<ConstantFilter>(); auto value = Constant(c.constant, numeric);
        if (key.null || value.null) return false;
        auto cmp = Compare(key, value, numeric);
        switch (c.comparison_type) {
        case ExpressionType::COMPARE_EQUAL: return cmp == 0;
        case ExpressionType::COMPARE_NOTEQUAL: return cmp != 0;
        case ExpressionType::COMPARE_LESSTHAN: return cmp < 0;
        case ExpressionType::COMPARE_LESSTHANOREQUALTO: return cmp <= 0;
        case ExpressionType::COMPARE_GREATERTHAN: return cmp > 0;
        case ExpressionType::COMPARE_GREATERTHANOREQUALTO: return cmp >= 0;
        default: throw NotImplementedException("fileindex comparison filter");
        }
    }
    case TableFilterType::IS_NULL: return key.null;
    case TableFilterType::IS_NOT_NULL: return !key.null;
    case TableFilterType::CONJUNCTION_AND:
        for (auto &child : filter.Cast<ConjunctionAndFilter>().child_filters) if (!Matches(*child, key, numeric)) return false;
        return true;
    case TableFilterType::CONJUNCTION_OR:
        for (auto &child : filter.Cast<ConjunctionOrFilter>().child_filters) if (Matches(*child, key, numeric)) return true;
        return false;
    case TableFilterType::IN_FILTER:
        for (auto &v : filter.Cast<InFilter>().values) if (!v.IsNull() && !key.null && Compare(key, Constant(v, numeric), numeric) == 0) return true;
        return false;
    // These are optimization hints. Their original predicates remain in DuckDB.
    case TableFilterType::OPTIONAL_FILTER: case TableFilterType::DYNAMIC_FILTER: return true;
    default: throw NotImplementedException("fileindex filter type");
    }
}
static void Bounds(const TableFilter &filter, bool numeric, vector<Bound> &bounds) {
    if (filter.filter_type == TableFilterType::CONJUNCTION_AND) {
        for (auto &child : filter.Cast<ConjunctionAndFilter>().child_filters) Bounds(*child, numeric, bounds);
    } else if (filter.filter_type == TableFilterType::CONSTANT_COMPARISON) {
        auto &c = filter.Cast<ConstantFilter>();
        if (!c.constant.IsNull() &&
            (c.comparison_type == ExpressionType::COMPARE_EQUAL || c.comparison_type == ExpressionType::COMPARE_LESSTHAN ||
             c.comparison_type == ExpressionType::COMPARE_LESSTHANOREQUALTO || c.comparison_type == ExpressionType::COMPARE_GREATERTHAN ||
             c.comparison_type == ExpressionType::COMPARE_GREATERTHANOREQUALTO)) {
            Key null; null.null = true;
            bounds.push_back({ExpressionType::COMPARE_GREATERTHAN, null});
            bounds.push_back({c.comparison_type, Constant(c.constant, numeric)});
        }
    } else if (filter.filter_type == TableFilterType::IS_NULL || filter.filter_type == TableFilterType::IS_NOT_NULL) {
        Key null; null.null = true;
        bounds.push_back({filter.filter_type == TableFilterType::IS_NULL ? ExpressionType::COMPARE_EQUAL : ExpressionType::COMPARE_GREATERTHAN, null});
    }
}
struct ScanState : public GlobalTableFunctionState {
    Index index;
    Reader reader;
    vector<column_t> columns;
    uint64_t current = 0, end = 0;
    bool indexed = false;
    vector<std::pair<size_t, unique_ptr<TableFilter>>> key_filters;
    vector<std::pair<column_t, unique_ptr<TableFilter>>> residual;
    uint64_t fetched = 0, bytes = 0;
    uint64_t prefix_columns = 0, entries_visited = 0;
    explicit ScanState(const ScanData &data) : index(data.index), reader(index.h) { }
};
static unique_ptr<GlobalTableFunctionState> InitScan(ClientContext &context, TableFunctionInitInput &input) {
    auto &data = input.bind_data->Cast<ScanData>();
    auto state = make_uniq<ScanState>(data);
    std::ostringstream original, current;
    WriteHeader(original, data.h); WriteHeader(current, state->index.h);
    Check(original.str() == current.str(), "index changed after binding; prepare the statement again");
    Validate(state->index.h, context, data.verify); state->columns = input.column_ids;
    if (input.filters) {
        for (auto &f : input.filters->filters) {
            Check(f.first < input.column_ids.size(), "invalid filter column");
            auto column = input.column_ids[f.first];
            size_t key = 0;
            while (key < data.h.keys.size() && data.h.keys[key].column != column) ++key;
            if (key < data.h.keys.size()) state->key_filters.emplace_back(key, f.second->Copy());
            else state->residual.emplace_back(column, f.second->Copy());
        }
    }
    state->end = data.h.rows;
    vector<Key> prefix;
    for (size_t component = 0; component < data.h.keys.size(); ++component) {
        const TableFilter *filter = nullptr;
        for (auto &f : state->key_filters) if (f.first == component) filter = f.second.get();
        if (!filter) break;
        vector<Bound> bounds; Bounds(*filter, data.h.keys[component].numeric, bounds);
        if (bounds.empty()) break;
        ++state->prefix_columns; state->indexed = true;
        bool equal = false; Key equality;
        for (auto &b : bounds) {
            auto bound = prefix; bound.push_back(b.key);
            auto lower = state->index.Bound(bound, false), upper = state->index.Bound(bound, true);
            switch (b.type) {
            case ExpressionType::COMPARE_EQUAL: state->current = std::max(state->current, lower); state->end = std::min(state->end, upper); break;
            case ExpressionType::COMPARE_LESSTHAN: state->end = std::min(state->end, lower); break;
            case ExpressionType::COMPARE_LESSTHANOREQUALTO: state->end = std::min(state->end, upper); break;
            case ExpressionType::COMPARE_GREATERTHAN: state->current = std::max(state->current, upper); break;
            case ExpressionType::COMPARE_GREATERTHANOREQUALTO: state->current = std::max(state->current, lower); break;
            default: throw InternalException("unexpected fileindex bound");
            }
            if (b.type == ExpressionType::COMPARE_EQUAL) { equal = true; equality = b.key; }
        }
        if (!equal || state->current >= state->end) break;
        prefix.push_back(std::move(equality));
    }
    // Broad ranges are cheaper in source order than as many random file seeks.
    if (state->indexed && state->end > state->current && state->end - state->current > 4096 &&
        state->end - state->current > data.h.rows / 4) {
        state->indexed = false; state->current = 0; state->end = data.h.rows;
        state->prefix_columns = 0;
    }
    if (!state->indexed && data.h.header) { vector<string> fields; uint64_t offset, length; state->reader.Next(fields, offset, length); }
    return state;
}
static void Scan(ClientContext &context, TableFunctionInput &input, DataChunk &output) {
    auto &state = input.global_state->Cast<ScanState>(); auto &h = state.index.h;
    Validate(h, context, false); idx_t count = 0;
    while (count < STANDARD_VECTOR_SIZE && state.current < state.end) {
        if ((state.current & 1023) == 0) Allowed(context);
        vector<string> fields;
        if (state.indexed) {
            auto entry = state.index.At(state.current); ++state.entries_visited;
            bool matches = true;
            for (auto &f : state.key_filters) if (!Matches(*f.second, entry.keys[f.first], h.keys[f.first].numeric)) { matches = false; break; }
            if (!matches) { ++state.current; continue; }
            fields = state.reader.At(entry.offset, entry.length); state.bytes += entry.length;
        } else { uint64_t offset, length; Check(state.reader.Next(fields, offset, length), "source ended before indexed row count"); state.bytes += length; }
        ++state.fetched;
        Check(fields.size() == h.names.size(), "source schema changed");
        bool matches = true;
        if (!state.indexed) {
            for (auto &f : state.key_filters) {
                if (!Matches(*f.second, MakeKey(fields[h.keys[f.first].column], h, f.first), h.keys[f.first].numeric)) { matches = false; break; }
            }
        }
        if (!matches) { ++state.current; continue; }
        for (auto &f : state.residual) {
            Key key; key.null = fields[f.first] == h.null_text;
            if (!key.null) key.text = fields[f.first];
            if (!Matches(*f.second, key, false)) { matches = false; break; }
        }
        if (!matches) { ++state.current; continue; }
        for (idx_t i = 0; i < state.columns.size(); ++i) {
            auto column = state.columns[i];
            if (column == COLUMN_IDENTIFIER_ROW_ID) output.SetValue(i, count, Value::BIGINT(static_cast<int64_t>(state.current)));
            else {
                auto &text = fields[column];
                if (text == h.null_text) output.SetValue(i, count, Value());
                else if (NumericColumn(h, column)) output.SetValue(i, count, Value::BIGINT(Integer(text)));
                else output.SetValue(i, count, Value(text));
            }
        }
        ++state.current; ++count;
    }
    Validate(h, context, false); output.SetCardinality(count);
}
static InsertionOrderPreservingMap<string> Explain(TableFunctionToStringInput &input) {
    auto &data = input.bind_data->Cast<ScanData>(); InsertionOrderPreservingMap<string> info;
    info["Index"] = data.index; info["Source"] = data.h.source;
    string columns;
    for (auto &key : data.h.keys) { if (!columns.empty()) columns += ", "; columns += data.h.names[key.column] + (key.numeric ? " BIGINT" : " VARCHAR"); }
    info["Indexed columns"] = columns;
    info["Access"] = "Persistent index for key filters; sequential otherwise"; return info;
}
static InsertionOrderPreservingMap<string> Actual(TableFunctionDynamicToStringInput &input) {
    InsertionOrderPreservingMap<string> info;
    if (input.global_state) {
        auto &state = input.global_state->Cast<ScanState>();
        info["Access"] = state.indexed ? "Persistent index lookup" : "Sequential source scan";
        info["Source records read"] = std::to_string(state.fetched);
        info["Source bytes read"] = std::to_string(state.bytes);
        info["Index prefix columns"] = std::to_string(state.prefix_columns);
        info["Index entries visited"] = std::to_string(state.entries_visited);
    }
    return info;
}
static unique_ptr<NodeStatistics> Cardinality(ClientContext &, const FunctionData *bind) {
    return make_uniq<NodeStatistics>(bind->Cast<ScanData>().h.rows);
}
static void Register(ExtensionLoader &loader) {
    TableFunction create("file_index_create", {LogicalType::VARCHAR, LogicalType::VARCHAR}, Create, BindCreate, InitOnce);
    create.named_parameters["format"] = LogicalType::VARCHAR;
    create.named_parameters["header"] = LogicalType::BOOLEAN;
    create.named_parameters["key_column"] = LogicalType::BIGINT;
    create.named_parameters["key_type"] = LogicalType::VARCHAR;
    create.named_parameters["key_columns"] = LogicalType::LIST(LogicalType::BIGINT);
    create.named_parameters["key_types"] = LogicalType::LIST(LogicalType::VARCHAR);
    create.named_parameters["delimiter"] = LogicalType::VARCHAR;
    create.named_parameters["nullstr"] = LogicalType::VARCHAR;
    create.named_parameters["record_length"] = LogicalType::BIGINT;
    create.named_parameters["widths"] = LogicalType::LIST(LogicalType::BIGINT);
    create.named_parameters["replace"] = LogicalType::BOOLEAN;
    create.named_parameters["memory_limit"] = LogicalType::VARCHAR;
    create.named_parameters["temp_limit"] = LogicalType::VARCHAR;
    loader.RegisterFunction(create);
    TableFunction scan("file_index_scan", {LogicalType::VARCHAR}, Scan, BindScan, InitScan);
    scan.named_parameters["verify"] = LogicalType::BOOLEAN;
    scan.projection_pushdown = true; scan.filter_pushdown = true;
    scan.to_string = Explain; scan.dynamic_to_string = Actual; scan.cardinality = Cardinality;
    loader.RegisterFunction(scan);
    loader.SetDescription("Persistent sidecar indexes for CSV and fixed-length UTF-8 files");
}
} // namespace fileindex

extern "C" {
DUCKDB_CPP_EXTENSION_ENTRY(fileindex, loader) { fileindex::Register(loader); }
}
