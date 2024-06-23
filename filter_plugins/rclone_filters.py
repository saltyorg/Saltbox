def filter_rclone_remote_name(item):
    if 'settings' in item and 'name' in item['settings'] and item['settings']['template'] == 'nfs':
        return item['settings']['name']
    else:
        remote = item['remote']
        return remote.split(':')[0] if ':' in remote else remote

def filter_rclone_remote_with_path(item):
    remote = item['remote']
    return remote if ':' in remote else remote + ':'

def filter_rclone_first_remote_name(rclone):
    remote = rclone['remotes'][0]['remote']
    return remote.split(':')[0] if ':' in remote else remote

def filter_rclone_first_remote_name_with_path(rclone):
    remote = rclone['remotes'][0]['remote']
    return remote if ':' in remote else remote + ':Media'

class FilterModule(object):
    def filters(self):
        return {
            'filter_rclone_remote_name': filter_rclone_remote_name,
            'filter_rclone_remote_with_path': filter_rclone_remote_with_path,
            'filter_rclone_first_remote_name': filter_rclone_first_remote_name,
            'filter_rclone_first_remote_name_with_path': filter_rclone_first_remote_name_with_path,
        }
