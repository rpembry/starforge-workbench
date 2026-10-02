"""Review the source-only GNOME presence extension without enabling it."""

import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / 'extensions/favorite-presence@rpembry.github.io'


def test_extension_metadata_and_dbus_surface():
    metadata = json.loads((EXTENSION / 'metadata.json').read_text())
    assert metadata['shell-version'] == ['50']
    assert metadata['session-modes'] == ['user']
    assert metadata['uuid'] == EXTENSION.name
    source = (EXTENSION / 'extension.js').read_text()
    xml = source.split('const INTERFACE_XML = `', 1)[1].split('`;', 1)[0]
    interface = ET.fromstring(xml).find('interface')
    assert interface is not None
    assert interface.attrib['name'] == 'org.starforge.Workbench.FavoritePresence'
    assert [(method.attrib['name'], [arg.attrib['type'] for arg in method])
            for method in interface.findall('method')] == [('Snapshot', ['t', 'a(sssu)'])]
    assert not interface.findall('signal') and not interface.findall('property')
    schema = ET.parse(EXTENSION / 'schemas/org.gnome.shell.extensions.starforge-favorite-presence.gschema.xml')
    assert schema.find('.//key[@name="favorites"]/default').text == '[]'


def test_mocked_presence_logic():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the mocked GNOME presence test')
    subprocess.run([node, '--test', str(ROOT / 'tests/gnome_favorite_presence.test.mjs')],
                   check=True, capture_output=True, text=True)


def test_shell_extension_syntax_without_loading_shell():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for the extension syntax check')
    subprocess.run([node, '--check', str(EXTENSION / 'extension.js')],
                   check=True, capture_output=True, text=True)


def test_pure_presence_module_in_gjs_without_loading_shell():
    gjs = shutil.which('gjs')
    if not gjs:
        pytest.skip('GJS is unavailable in this test environment')
    subprocess.run([gjs, '-m', str(ROOT / 'tests/gnome_favorite_presence_gjs.js')],
                   check=True, capture_output=True, text=True)
