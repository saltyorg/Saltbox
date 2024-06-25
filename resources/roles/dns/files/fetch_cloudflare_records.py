import argparse
import cloudflare
from cloudflare import Cloudflare
import json
import sys
import datetime
from typing import List, cast


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        return json.JSONEncoder.default(self, obj)


def fetch_dns_records(client: Cloudflare, zone_id: str, record_name: str) -> list:
    try:
        records = client.dns.records.list(zone_id=zone_id, name=record_name).to_dict()
        results = cast(List[dict], records["result"])
        return results
    except cloudflare.APIConnectionError as e:
        print(f'Error fetching DNS records: {e}', file=sys.stderr)
        raise
    except Exception as e:
        print(f'Unexpected error: {e}', file=sys.stderr)
        raise


def get_zone_id(client: Cloudflare, zone_name: str) -> str:
    try:
        zone = client.zones.list(name=zone_name)
        if len(zone.result) == 0:
            raise ValueError(f'Specified zone: {zone_name} was not found')
        return zone.result[0].id
    except cloudflare.APIConnectionError as e:
        print(f'Error fetching zone ID: {e}', file=sys.stderr)
        raise
    except Exception as e:
        print(f'Unexpected error: {e}', file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(
        prog='Saltbox Cloudflare Helper',
        description='Parses Cloudflare Zone Records',
        epilog='')

    parser.add_argument('--auth_key', required=True, help='API key for Cloudflare')
    parser.add_argument('--auth_email', required=True, help='Email associated with Cloudflare account')
    parser.add_argument('--zone_name', required=True, help='Name of the Cloudflare zone')
    parser.add_argument('--record', required=True, help='DNS record to fetch')

    args = parser.parse_args()

    cf = Cloudflare(api_email=args.auth_email, api_key=args.auth_key)

    try:
        zone_id = get_zone_id(cf, args.zone_name)
        print(json.dumps(fetch_dns_records(cf, zone_id, args.record), cls=CustomJSONEncoder))
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        exit(1)
    except Exception as e:
        print(f'Failed to fetch DNS records: {e}', file=sys.stderr)
        exit(1)

    exit(0)


if __name__ == '__main__':
    main()
