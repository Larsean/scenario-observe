import argparse
import sys

from observe import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="observe")
    commands = parser.add_subparsers(dest="command", required=True)
    render_parser = commands.add_parser("render", help="render a JSONL trace as offline HTML")
    render_parser.add_argument("input", help="path to a schema-v1 JSONL trace")
    render_parser.add_argument("--output", help="HTML output path; defaults to the input path with .html")
    arguments = parser.parse_args(argv)
    try:
        output_path = render(arguments.input, arguments.output)
    except (OSError, ValueError) as error:
        parser.exit(2, f"observe: error: {error}\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
