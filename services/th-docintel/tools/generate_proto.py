from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
PROTO_DIRECTORY = ROOT / "proto"
OUTPUT_DIRECTORY = ROOT / "tender_docintel" / "proto"
PROTO_FILE = PROTO_DIRECTORY / "docintel.proto"


def main() -> None:
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{PROTO_DIRECTORY}",
            f"--python_out={OUTPUT_DIRECTORY}",
            f"--pyi_out={OUTPUT_DIRECTORY}",
            f"--grpc_python_out={OUTPUT_DIRECTORY}",
            str(PROTO_FILE),
        ]
    )
    if result != 0:
        raise SystemExit(result)

    grpc_module = OUTPUT_DIRECTORY / "docintel_pb2_grpc.py"
    contents = grpc_module.read_text(encoding="utf-8")
    contents = contents.replace(
        "import docintel_pb2 as docintel__pb2",
        "from tender_docintel.proto import docintel_pb2 as docintel__pb2",
    )
    contents = contents.replace(
        "return grpc.experimental.unary_unary(",
        "return grpc.experimental.unary_unary(  # type: ignore[attr-defined]",
    )
    grpc_module.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
