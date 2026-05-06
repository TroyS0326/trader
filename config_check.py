import argparse
import sys
import config

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    errors = config.validate_required_production_config(strict=args.strict)
    if errors:
        for e in errors:
            print(f'ERROR: {e}')
        sys.exit(1)
    print('Production config validation passed.')
