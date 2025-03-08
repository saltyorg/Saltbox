def traefik_host_rule(host, host_override, fqdn_override_list):
    """
    Ansible filter to generate Traefik host rule.

    Args:
        host (str): The base host.
        host_override (str): The host override.
        fqdn_override_list (list): The FQDN override list.

    Returns:
        str: The generated Traefik host rule.
    """
    if host_override:
        return f'({host_override})'
    elif fqdn_override_list:
        formatted_fqdn_overrides = [f"Host(`{fqdn}`)" for fqdn in fqdn_override_list]
        fqdn_override_string = " || ".join(formatted_fqdn_overrides)
        return f"({fqdn_override_string})"
    else:
        return f"Host(`{host}`)"

class FilterModule(object):
    """ Ansible filter module """

    def filters(self):
        """ return list of filters """
        return {
            'traefik_host_rule': traefik_host_rule,
        }
