from ansible.errors import AnsibleFilterError
import os
import configparser

class FilterModule(object):
    def filters(self):
        return {
            'check_plex_ini': self.check_plex_ini
        }

    def check_plex_ini(self, file_path, plex_name):
        if not os.path.exists(file_path):
            return {'exists': False, 'identifier': '', 'token': ''}

        config = configparser.ConfigParser()
        config.read(file_path)

        if plex_name not in config.sections():
            return {'exists': True, 'identifier': '', 'token': ''}

        section = config[plex_name]
        return {
            'exists': True,
            'identifier': section.get('client_identifier', ''),
            'token': section.get('token', '')
        }
