import argparse
import CloudFlare


def get_ssl_tls_mode(auth_email, auth_key, zone_name):
    cf = CloudFlare.CloudFlare(email=auth_email, key=auth_key)

    # Query for the zone name and expect only one value back
    try:
        zones = cf.zones.get(params={'name': zone_name, 'per_page': 1})
    except CloudFlare.exceptions.CloudFlareAPIError as e:
        exit(f'/zones.get {e.code} {e} - API call failed')
    except Exception as e:
        exit(f'/zones.get - {e} - API call failed')

    if len(zones) == 0:
        exit('No zones found')

    # Extract the zone_id which is needed to process that zone
    zone = zones[0]
    zone_id = zone['id']

    # Get the SSL/TLS settings for the zone
    try:
        ssl_settings = cf.zones.settings.ssl.get(zone_id)
        ssl_mode = ssl_settings['value']
        print(f"{ssl_mode}")
    except CloudFlare.exceptions.CloudFlareAPIError as e:
        exit(f'/zones/settings/ssl.get {e.code} {e} - API call failed')
    except Exception as e:
        exit(f'/zones/settings/ssl.get - {e} - API call failed')


def main():
    parser = argparse.ArgumentParser(
        prog='SSL/TLS Mode Checker',
        description='Retrieves Cloudflare SSL/TLS Encryption Mode for a Zone',
        epilog='')

    parser.add_argument('--auth_key', required=True, help='Cloudflare API key')
    parser.add_argument('--auth_email', required=True, help='Cloudflare account email')
    parser.add_argument('--zone_name', required=True, help='Name of the Cloudflare zone')

    args = parser.parse_args()

    get_ssl_tls_mode(args.auth_email, args.auth_key, args.zone_name)


if __name__ == '__main__':
    main()
