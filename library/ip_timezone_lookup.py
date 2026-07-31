# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: ip_timezone_lookup
description:
    - Fetches timezone information for a public IP address from multiple IP geolocation services.
    - Returns a consensus timezone when multiple sources agree.
    - Provides individual results from each source for verification.
    - Returns only installed IANA timezone identifiers suitable for C(timedatectl).
author: salty
options:
    ip_address:
        description:
            - Public IPv4 or IPv6 address to look up.
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
            - Minimum number of sources that must agree on a unique result.
            - Must not exceed the number of configured sources.
        required: false
        default: 2
        type: int
"""

EXAMPLES = """
- name: Get timezone for specific IP
  ip_timezone_lookup:
    ip_address: "{{ ip_address_public }}"
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
"""

RETURN = """
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
    sample: 11
total_sources:
    description: Total number of sources queried
    type: int
    returned: always
    sample: 11
successful_lookups:
    description: Number of successful API calls returning valid timezones
    type: int
    returned: always
    sample: 11
sources:
    description: Dictionary containing results from each source
    type: dict
    returned: always
    sample: {
        "ipapi_co": {"timezone": "Europe/Helsinki", "success": true},
        "ipinfo": {"timezone": "Europe/Helsinki", "success": true}
    }
ip_used:
    description: The IP address that was looked up
    type: str
    returned: always
    sample: "90.130.70.73"
"""

import asyncio
from collections import Counter
import ipaddress
import json
import os
from typing import Any, Awaitable, Callable, Optional

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

try:
    import aiohttp
except ImportError as import_error:
    aiohttp = None
    AIOHTTP_IMPORT_ERROR = str(import_error)
else:
    AIOHTTP_IMPORT_ERROR = None


ZONEINFO_ROOT = '/usr/share/zoneinfo'
EXCLUDED_ZONEINFO_PREFIXES = ('posix/', 'right/')


class LookupRequestError(Exception):
    """A concise error suitable for an individual source result."""


def validate_timezone(timezone: object) -> Optional[str]:
    """Return an installed IANA timezone identifier, or None."""
    if not isinstance(timezone, str):
        return None

    timezone = timezone.strip()
    if not timezone or '\x00' in timezone:
        return None

    normalized = os.path.normpath(timezone)
    if (
        normalized != timezone
        or os.path.isabs(normalized)
        or normalized == '..'
        or normalized.startswith('../')
        or normalized.startswith(EXCLUDED_ZONEINFO_PREFIXES)
    ):
        return None

    zoneinfo_root = os.path.realpath(ZONEINFO_ROOT)
    zoneinfo_path = os.path.realpath(os.path.join(zoneinfo_root, normalized))
    try:
        if os.path.commonpath((zoneinfo_root, zoneinfo_path)) != zoneinfo_root:
            return None
        with open(zoneinfo_path, 'rb') as timezone_file:
            return timezone if timezone_file.read(4) == b'TZif' else None
    except (OSError, ValueError):
        return None


class IPTimezoneLookup:
    SOURCE_METHODS = (
        ('ipinfo', 'fetch_ipinfo'),
        ('ipapi_co', 'fetch_ipapi_co'),
        ('freegeoip', 'fetch_freegeoip'),
        ('ipwhois', 'fetch_ipwhois'),
        ('geojs', 'fetch_geojs'),
        ('ipregistry', 'fetch_ipregistry'),
        ('ipapi_is', 'fetch_ipapi_is'),
        ('ipwho_is', 'fetch_ipwho_is'),
        ('ipquery', 'fetch_ipquery'),
        ('reallyfreegeoip', 'fetch_reallyfreegeoip'),
        ('ipaddress_to', 'fetch_ipaddress_to'),
    )

    def __init__(self, module: AnsibleModule) -> None:
        self.module: AnsibleModule = module
        self.ip_address: str = module.params['ip_address']
        self.timeout: int = module.params['timeout']
        self.min_consensus: int = module.params['min_consensus']
        self.results: dict[str, dict[str, object]] = {}
        
    async def make_request(self, session: aiohttp.ClientSession, url: str, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Make an HTTP request and retain a useful failure reason."""
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status != 200:
                    raise LookupRequestError(f"HTTP {response.status}")
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as error:
                    raise LookupRequestError("Invalid JSON response") from error
                if not isinstance(data, dict):
                    raise LookupRequestError("JSON response was not an object")
                return data
        except asyncio.TimeoutError as error:
            raise LookupRequestError(f"Request timed out after {self.timeout} seconds") from error
        except LookupRequestError:
            raise
        except aiohttp.ClientError as error:
            raise LookupRequestError(f"Request failed: {error}") from error

    async def fetch_ipinfo(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipinfo.io (free tier, reliable)"""
        url = f"https://ipinfo.io/{self.ip_address}/json"
        data = await self.make_request(session, url)
        if data:
            return data.get('timezone')
        return None

    async def fetch_ipapi_co(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipapi.co (reliable)"""
        url = f"https://ipapi.co/{self.ip_address}/json/"
        data = await self.make_request(session, url)
        if data and not data.get('error'):
            return data.get('timezone')
        return None

    async def fetch_freegeoip(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from freegeoip.app (reliable)"""
        url = f"https://freegeoip.app/json/{self.ip_address}"
        data = await self.make_request(session, url)
        if data:
            return data.get('time_zone')
        return None

    async def fetch_ipwhois(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipwhois.app (reliable)"""
        url = f"https://ipwhois.app/json/{self.ip_address}"
        data = await self.make_request(session, url)
        if data and data.get('success') != False:
            return data.get('timezone')
        return None

    async def fetch_geojs(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from geojs.io (reliable)"""
        url = f"https://get.geojs.io/v1/ip/geo/{self.ip_address}.json"
        data = await self.make_request(session, url)
        if data:
            return data.get('timezone')
        return None

    async def fetch_ipregistry(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipregistry.co (reliable with tryout key)"""
        url = f"https://api.ipregistry.co/{self.ip_address}?key=tryout"
        data = await self.make_request(session, url)
        if data:
            tz_info = data.get('time_zone')
            if tz_info:
                return tz_info.get('id')
        return None

    async def fetch_ipapi_is(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipapi.is (reliable)"""
        url = f"https://api.ipapi.is/?q={self.ip_address}"
        data = await self.make_request(session, url)
        if data:
            location = data.get('location')
            if location:
                return location.get('timezone')
        return None

    async def fetch_ipwho_is(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipwho.is."""
        url = f"https://ipwho.is/{self.ip_address}"
        data = await self.make_request(session, url)
        if data.get('success'):
            timezone = data.get('timezone')
            if isinstance(timezone, dict):
                return timezone.get('id')
        return None

    async def fetch_ipquery(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipquery.io."""
        url = f"https://api.ipquery.io/{self.ip_address}"
        data = await self.make_request(session, url)
        location = data.get('location')
        if isinstance(location, dict):
            return location.get('timezone')
        return None

    async def fetch_reallyfreegeoip(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from reallyfreegeoip.org."""
        url = f"https://reallyfreegeoip.org/json/{self.ip_address}"
        data = await self.make_request(session, url)
        return data.get('time_zone')

    async def fetch_ipaddress_to(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch from ipaddress.to."""
        url = f"https://ipaddress.to/api/lookup/{self.ip_address}"
        data = await self.make_request(session, url)
        if data.get('success'):
            location = data.get('location')
            if isinstance(location, dict):
                return location.get('timezone')
        return None
    
    async def _fetch_from_source(
        self,
        session: aiohttp.ClientSession,
        source_name: str,
        lookup_func: Callable[[aiohttp.ClientSession], Awaitable[Optional[str]]]
    ) -> tuple[str, dict[str, object]]:
        """Fetch timezone from a single source with error handling"""
        try:
            timezone = await lookup_func(session)
            validated_timezone = validate_timezone(timezone)
            if validated_timezone:
                return source_name, {
                    'timezone': validated_timezone,
                    'success': True
                }
            return source_name, {
                'timezone': None,
                'success': False,
                'error': 'No installed IANA timezone returned'
            }
        except LookupRequestError as error:
            return source_name, {
                'timezone': None,
                'success': False,
                'error': str(error)
            }
        except Exception as e:
            return source_name, {
                'timezone': None,
                'success': False,
                'error': str(e)
            }

    async def _run_lookups_async(self) -> None:
        """Run all timezone lookups concurrently"""
        lookup_methods = {
            source_name: getattr(self, method_name)
            for source_name, method_name in self.SOURCE_METHODS
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

    def run_lookups(self) -> None:
        """Run all timezone lookups (synchronous wrapper for async operations)"""
        asyncio.run(self._run_lookups_async())
    
    def determine_consensus(self) -> tuple[Optional[str], str, int]:
        """Determine the consensus timezone"""
        # Collect all successful timezones
        timezones = []
        
        for source, result in self.results.items():
            if result['success'] and result['timezone']:
                timezones.append(result['timezone'])
        
        if not timezones:
            return None, 'none', 0
        
        tz_counter = Counter(timezones)
        consensus_count = max(tz_counter.values())
        
        if consensus_count < self.min_consensus:
            return None, 'low', consensus_count

        consensus_timezones = [
            timezone
            for timezone, count in tz_counter.items()
            if count == consensus_count
        ]
        if len(consensus_timezones) != 1:
            return None, 'low', consensus_count

        consensus_tz = consensus_timezones[0]

        # Determine confidence
        total_valid = len(timezones)
        if consensus_count >= total_valid * 0.7:
            confidence = 'high'
        elif consensus_count >= total_valid * 0.5:
            confidence = 'medium'
        else:
            confidence = 'low'

        return consensus_tz, confidence, consensus_count

def main() -> None:
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
    if module.params['min_consensus'] > len(IPTimezoneLookup.SOURCE_METHODS):
        module.fail_json(
            msg=f"min_consensus must not exceed the {len(IPTimezoneLookup.SOURCE_METHODS)} configured sources"
        )

    if aiohttp is None:
        module.fail_json(
            msg=missing_required_lib('aiohttp'),
            exception=AIOHTTP_IMPORT_ERROR
        )

    try:
        ip_address = ipaddress.ip_address(module.params['ip_address'])
    except ValueError as error:
        module.fail_json(msg=f"ip_address must be a valid public IPv4 or IPv6 address: {error}")
    if not ip_address.is_global:
        module.fail_json(msg=f"ip_address '{ip_address}' is not a public IP address")
    module.params['ip_address'] = str(ip_address)
    
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

    if successful_lookups == 0:
        module.fail_json(msg="Could not determine timezone from any source", **result)

    if consensus_count >= lookup.min_consensus:
        module.fail_json(
            msg="Could not determine a unique timezone consensus",
            **result
        )

    module.fail_json(
        msg=f"Could not reach minimum consensus of {lookup.min_consensus}",
        **result
    )

if __name__ == '__main__':
    main()
