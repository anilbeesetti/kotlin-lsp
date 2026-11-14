import runpy
import sys
import tempfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'script'))
build = runpy.run_path(str(ROOT / 'script/build-runtime'))
DESCRIPTOR = 'META-INF/language-server/features/javaBase/lsApiJavaBaseImpl.xml'


class JavaRenameDescriptorTests(unittest.TestCase):
    def test_native_processors_are_scoped_to_intellij_and_other_entries_survive(self):
        original = b'<idea-plugin><extensions defaultExtensionNs="com.intellij"><existing/></extensions><extensions defaultExtensionNs="ls"><other/></extensions></idea-plugin>'
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / 'feature.jar'
            with zipfile.ZipFile(jar, 'w') as archive:
                archive.writestr(DESCRIPTOR, original)
                archive.writestr('Existing.class', b'unchanged class bytes')
            build['register_java_headless_rename'](jar)
            with zipfile.ZipFile(jar) as archive:
                root = ET.fromstring(archive.read(DESCRIPTOR))
                self.assertEqual(archive.read('Existing.class'), b'unchanged class bytes')
            extensions = root.find("extensions[@defaultExtensionNs='com.intellij']")
            self.assertIsNotNone(extensions.find('existing'))
            self.assertEqual(extensions.find('headlessRenameSupportedLanguage').get('language'), 'JAVA')
            processors = extensions.findall('headlessRenamePsiElementProcessor')
            self.assertEqual(len(processors), 3)
            self.assertTrue(all('$HeadlessRenameJava' in item.get('implementation') for item in processors))
            self.assertIsNone(extensions.find('renamePsiElementProcessor'))
            self.assertEqual(len(root.find("extensions[@defaultExtensionNs='ls']")), 1)
            with self.assertRaisesRegex(RuntimeError, 'review native registrations'):
                build['register_java_headless_rename'](jar)

    def test_changed_upstream_descriptor_blocks_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / 'feature.jar'
            with zipfile.ZipFile(jar, 'w') as archive:
                archive.writestr(DESCRIPTOR, b'<idea-plugin/>')
            original = jar.read_bytes()
            with self.assertRaises(RuntimeError):
                build['register_java_headless_rename'](jar)
            self.assertEqual(jar.read_bytes(), original)
