#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = '''
---
module: ip_timezone_lookup
short_description: Fetch timezone based on IP address from multiple sources
description:
    - Fetches timezone information from multiple IP geolocation services
    - Returns a consensus timezone when multiple sources agree
    - Provides individual results from each source for verification
    - Returns only valid IANA timezone identifiers suitable for timedatectl
version_added: "2.9"
author: "Custom Module"
options:
    ip_address:
        description:
            - IP address to lookup timezone for
        required: true
        type: str
    timeout:
        description:
            - Timeout in seconds for each API request
        required: false
        default: 5
        type: int
    min_consensus:
        description:
            - Minimum number of sources that must agree for consensus
        required: false
        default: 2
        type: int
'''

EXAMPLES = '''
- name: Get timezone for specific IP
  ip_timezone_lookup:
    ip_address: "8.8.8.8"
  register: tz_result

- name: Set system timezone based on IP location
  command: timedatectl set-timezone {{ tz_result.timezone }}
  when: tz_result.confidence == 'high'

- name: Display consensus timezone
  debug:
    msg: "Consensus timezone: {{ tz_result.timezone }}"

- name: Display all source results
  debug:
    msg: "{{ tz_result.sources }}"
'''

RETURN = '''
timezone:
    description: The consensus timezone in IANA format (suitable for timedatectl)
    type: str
    returned: always
    sample: "Europe/Helsinki"
confidence:
    description: Confidence level of the result (high/medium/low/none)
    type: str
    returned: always
    sample: "high"
consensus_count:
    description: Number of sources agreeing on the timezone
    type: int
    returned: always
    sample: 8
total_sources:
    description: Total number of sources queried
    type: int
    returned: always
    sample: 8
successful_lookups:
    description: Number of successful API calls returning valid timezones
    type: int
    returned: always
    sample: 8
sources:
    description: Dictionary containing results from each source
    type: dict
    returned: always
    sample: {
        "ipapi": {"timezone": "Europe/Helsinki", "success": true},
        "ipinfo": {"timezone": "Europe/Helsinki", "success": true}
    }
ip_used:
    description: The IP address that was looked up
    type: str
    returned: always
    sample: "8.8.8.8"
'''

from ansible.module_utils.basic import AnsibleModule
import json
from collections import Counter
import asyncio
import aiohttp

class IPTimezoneLookup:
    def __init__(self, module):
        self.module = module
        self.ip_address = module.params['ip_address']
        self.timeout = module.params['timeout']
        self.min_consensus = module.params['min_consensus']
        self.results = {}
        
    async def make_request(self, session, url, headers=None):
        """Make async HTTP request with error handling"""
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status == 200:
                    return await response.json()
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            return None
    
    async def fetch_ipapi(self, session):
        """Fetch from ip-api.com (free, reliable)"""
        url = f"http://ip-api.com/json/{self.ip_address}"
        data = await self.make_request(session, url)
        if data and data.get('status') == 'success':
            return data.get('timezone')
        return None

    async def fetch_ipinfo(self, session):
        """Fetch from ipinfo.io (free tier, reliable)"""
        url = f"https://ipinfo.io/{self.ip_address}/json"
        data = await self.make_request(session, url)
        if data:
            return data.get('timezone')
        return None

    async def fetch_ipapi_co(self, session):
        """Fetch from ipapi.co (reliable)"""
        url = f"https://ipapi.co/{self.ip_address}/json/"
        data = await self.make_request(session, url)
        if data and not data.get('error'):
            return data.get('timezone')
        return None

    async def fetch_freegeoip(self, session):
        """Fetch from freegeoip.app (reliable)"""
        url = f"https://freegeoip.app/json/{self.ip_address}"
        data = await self.make_request(session, url)
        if data:
            return data.get('time_zone')
        return None

    async def fetch_ipwhois(self, session):
        """Fetch from ipwhois.app (reliable)"""
        url = f"https://ipwhois.app/json/{self.ip_address}"
        data = await self.make_request(session, url)
        if data and data.get('success') != False:
            return data.get('timezone')
        return None

    async def fetch_geojs(self, session):
        """Fetch from geojs.io (reliable)"""
        url = f"https://get.geojs.io/v1/ip/geo/{self.ip_address}.json"
        data = await self.make_request(session, url)
        if data:
            return data.get('timezone')
        return None

    async def fetch_ipregistry(self, session):
        """Fetch from ipregistry.co (reliable with tryout key)"""
        url = f"https://api.ipregistry.co/{self.ip_address}?key=tryout"
        data = await self.make_request(session, url)
        if data:
            tz_info = data.get('time_zone')
            if tz_info:
                return tz_info.get('id')
        return None

    async def fetch_ipapi_is(self, session):
        """Fetch from ipapi.is (reliable)"""
        url = f"https://api.ipapi.is/?q={self.ip_address}"
        data = await self.make_request(session, url)
        if data:
            location = data.get('location')
            if location:
                return location.get('timezone')
        return None
    
    async def _fetch_from_source(self, session, source_name, lookup_func):
        """Fetch timezone from a single source with error handling"""
        try:
            timezone = await lookup_func(session)
            if timezone and '/' in timezone:  # Valid IANA timezone
                return source_name, {
                    'timezone': timezone,
                    'success': True
                }
            else:
                return source_name, {
                    'timezone': None,
                    'success': False,
                    'error': 'No valid IANA timezone returned'
                }
        except Exception as e:
            return source_name, {
                'timezone': None,
                'success': False,
                'error': str(e)
            }

    async def _run_lookups_async(self):
        """Run all timezone lookups concurrently"""
        # Only include sources that returned valid results in testing
        lookup_methods = {
            'ipapi': self.fetch_ipapi,
            'ipinfo': self.fetch_ipinfo,
            'ipapi_co': self.fetch_ipapi_co,
            'freegeoip': self.fetch_freegeoip,
            'ipwhois': self.fetch_ipwhois,
            'geojs': self.fetch_geojs,
            'ipregistry': self.fetch_ipregistry,
            'ipapi_is': self.fetch_ipapi_is,
        }

        # Create aiohttp session and run all lookups concurrently
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._fetch_from_source(session, source_name, lookup_func)
                for source_name, lookup_func in lookup_methods.items()
            ]
            results = await asyncio.gather(*tasks)

            # Store results
            for source_name, result in results:
                self.results[source_name] = result

    def run_lookups(self):
        """Run all timezone lookups (synchronous wrapper for async operations)"""
        asyncio.run(self._run_lookups_async())
    
    def determine_consensus(self):
        """Determine the consensus timezone"""
        # Collect all successful timezones
        timezones = []
        
        for source, result in self.results.items():
            if result['success'] and result['timezone']:
                timezones.append(result['timezone'])
        
        if not timezones:
            return None, 'none', 0
        
        # Count occurrences
        tz_counter = Counter(timezones)
        most_common = tz_counter.most_common(1)[0]
        consensus_tz = most_common[0]
        consensus_count = most_common[1]
        
        # Determine confidence
        total_valid = len(timezones)
        if consensus_count >= self.min_consensus:
            if consensus_count >= total_valid * 0.7:
                confidence = 'high'
            elif consensus_count >= total_valid * 0.5:
                confidence = 'medium'
            else:
                confidence = 'low'
        else:
            confidence = 'low'
        
        return consensus_tz, confidence, consensus_count

def main():
    module = AnsibleModule(
        argument_spec=dict(
            ip_address=dict(type='str', required=True),
            timeout=dict(type='int', default=5),
            min_consensus=dict(type='int', default=2)
        ),
        supports_check_mode=True
    )

    if module.params['timeout'] <= 0:
        module.fail_json(msg="timeout must be a positive integer")
    if module.params['min_consensus'] < 1:
        module.fail_json(msg="min_consensus must be at least 1")

    if module.check_mode:
        module.exit_json(changed=False)
    
    lookup = IPTimezoneLookup(module)
    lookup.run_lookups()
    
    consensus_tz, confidence, consensus_count = lookup.determine_consensus()
    
    successful_lookups = sum(1 for r in lookup.results.values() if r['success'])
    
    result = {
        'changed': False,
        'timezone': consensus_tz,
        'confidence': confidence,
        'consensus_count': consensus_count,
        'total_sources': len(lookup.results),
        'successful_lookups': successful_lookups,
        'sources': lookup.results,
        'ip_used': lookup.ip_address
    }
    
    if consensus_tz:
        module.exit_json(**result)
    else:
        module.fail_json(msg="Could not determine timezone from any source", **result)

if __name__ == '__main__':
    main()
