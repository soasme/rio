import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upper", action="store_true")
    parser.add_argument("text")
    args = parser.parse_args()
    print(args.text.upper())


if __name__ == "__main__":
    main()
