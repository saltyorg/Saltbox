import argparse
import cloudflare
from cloudflare import Cloudflare


def get_zone_id(client: Cloudflare, zone_name: str) -> str:
    try:
        zone = client.zones.list(name=zone_name)
        if len(zone.result) == 0:
            raise ValueError(f'Specified zone: {zone_name} was not found')
        return zone.result[0].id
    except cloudflare.APIConnectionError as e:
        exit(f'Error fetching zone ID: {e}')
    except Exception as e:
        exit(f'Unexpected error: {e}')


def get_ssl_tls_mode(auth_email, auth_key, zone_name):
    cf = Cloudflare(api_email=auth_email, api_key=auth_key)

    zone_id = get_zone_id(cf, zone_name)

    # Get the SSL/TLS settings for the zone
    try:
        ssl_settings = cf.zones.settings.get(setting_id='ssl', zone_id=zone_id).to_dict()
        ssl_mode = ssl_settings['value']
        print(f"{ssl_mode}")
    except cloudflare.APIConnectionError as e:
        exit(f'/zones/settings/ssl.get - {e} - API call failed')
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
