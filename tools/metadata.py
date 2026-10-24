"""Append DuckDB v1.4.1's unsigned C++ extension metadata footer."""
import pathlib
import sys


def field(value):
    data = value.encode("ascii")
    if len(data) > 32:
        raise ValueError("metadata field too long")
    return data.ljust(32, b"\0")


if __name__ == "__main__":
    fields = ["4", sys.argv[2], "v1.4.1", "0.2.0", "CPP", "", "", ""]
    footer = b"\0\x93\x04\x10duckdb_signature\x80\x04"
    footer += b"".join(field(s) for s in reversed(fields)) + bytes(256)
    with pathlib.Path(sys.argv[1]).open("ab") as output:
        output.write(footer)
