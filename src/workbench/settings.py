"""External personal display and reporting settings."""
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


@dataclass(frozen=True)
class PersonalSettings:
    human_name: str = 'Operator'
    reporting_timezone: str = 'America/New_York'

    @property
    def zone(self):
        return ZoneInfo(self.reporting_timezone)


DEFAULT_SETTINGS = PersonalSettings()


def load_settings(path=None):
    """Load a small, private YAML file without exposing its contents in errors."""
    path = path or os.environ.get('WB_SETTINGS_FILE')
    if not path:
        return DEFAULT_SETTINGS
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError('WB_SETTINGS_FILE must be an absolute path')
    try:
        text = path.read_text()
    except OSError:
        raise RuntimeError('Unable to load Workbench settings file') from None
    except UnicodeError:
        raise RuntimeError('Unable to decode Workbench settings file as text') from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise RuntimeError('Unable to parse Workbench settings YAML') from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RuntimeError('Workbench settings must be a YAML mapping')
    unknown = set(data) - {'human_name', 'reporting_timezone'}
    if unknown:
        raise RuntimeError('Workbench settings contain an unsupported field')
    human_name = data.get('human_name', DEFAULT_SETTINGS.human_name)
    timezone = data.get('reporting_timezone', DEFAULT_SETTINGS.reporting_timezone)
    if not isinstance(human_name, str) or not 1 <= len(human_name.strip()) <= 500:
        raise RuntimeError('Workbench setting human_name must be a non-empty value up to 500 characters')
    if not isinstance(timezone, str):
        raise RuntimeError('Workbench setting reporting_timezone must be an IANA timezone name')
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, UnicodeError):
        raise RuntimeError('Workbench setting reporting_timezone must be a valid IANA timezone name') from None
    return PersonalSettings(human_name=human_name.strip(), reporting_timezone=timezone)
