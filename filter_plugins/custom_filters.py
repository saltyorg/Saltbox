def saltbox_facts_default(value, default_value=''):
    """Replace an arbitrary 'no value' indicator with a default value."""
    if value == "saltbox_fact_missing":
        return default_value
    return value

class FilterModule(object):
    def filters(self):
        return {
            'saltbox_facts_default': saltbox_facts_default,
        }
