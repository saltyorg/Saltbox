import argparse
import CloudFlare
import json


def main():
    parser = argparse.ArgumentParser(
        prog='Saltbox Cloudflare Helper',
        description='Parses Cloudflare Zone Records',
        epilog='')

    parser.add_argument('--auth_key', required=True)
    parser.add_argument('--auth_email', required=True)
    parser.add_argument('--zone_name', required=True)
    parser.add_argument('--record', required=True)

    args = parser.parse_args()

    cf = CloudFlare.CloudFlare(email=args.auth_email, key=args.auth_key, raw=True)

    # query for the zone name and expect only one value back
    try:
        zones = cf.zones.get(params={'name': args.zone_name, 'per_page': 1})
    except CloudFlare.exceptions.CloudFlareAPIError as e:
        exit('/zones.get %d %s - api call failed' % (e, e))
    except Exception as e:
        exit(f'/zones.get - {e} - api call failed')

    if len(zones["result"]) == 0:
        exit('No zones found')

    # extract the zone_id which is needed to process that zone
    zone = zones["result"][0]
    zone_id = zone['id']

    dns_records = []

    # request the DNS records from that zone
    try:
        page_number = 0

        while True:
            page_number += 1
            raw_results = cf.zones.dns_records.get(zone_id, params={'name': args.record, 'per_page': 100, 'page': page_number})
            dns_records = dns_records + raw_results['result']

            total_pages = raw_results['result_info']['total_pages']

            if page_number == total_pages:
                break

    except CloudFlare.exceptions.CloudFlareAPIError as e:
        exit('/zones/dns_records.get %d %s - api call failed' % (e, e))

    print(json.dumps(dns_records))

    exit(0)


if __name__ == '__main__':
    main()
