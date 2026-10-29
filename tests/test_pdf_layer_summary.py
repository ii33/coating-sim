"""PDF presentation regressions: portable suggestions and repeated layer tables."""
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import warnings

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from coat_sim.core.result_pdf import (suggested_pdf_filename, _process_layer_rows,
    _process_pages, _detail_sections, _font, export_result_pdf)
from coat_sim.core import result_pdf


class PdfLayerSummaryTests(unittest.TestCase):
    def fixture(self, count=65):
        process = {'name': '增透膜测试', 'steps': [{'source_slot': 1, 'target_nm': 100+i,
            'rate_nm_s': .5, 'target_mode': 'monitor', 'monitor_slot': 2} for i in range(count)]}
        machine = {'sources': [{'file': '二氧化硅.coatsource',
                    'profile': {'material': {'name': '二氧化硅 SiO₂'}}}]}
        result = {'field_nm': np.ones((3,3)), 'x': np.arange(3.), 'y': np.arange(3.),
                  'total_target_nm': 1000, 'mean_nm': 1., 'cv_percent': .5}
        return process, machine, result

    def test_safe_concise_suggestion(self):
        for title in ['CON', 'AUX.foo', 'COM¹', '../ unsafe:*?\\name \n', '\u202e'+'测试'*200, '']:
            name = suggested_pdf_filename({'total_target_nm': 1200}, {'name': title, 'steps': [{},{}]})
            self.assertTrue(name.endswith('.pdf'))
            self.assertLessEqual(len(name.encode('utf-8')), 180)
            self.assertFalse(re.search(r'[<>:"/\\|?*\x00-\x1f]', name))
            self.assertNotIn('\u202e', name)
            self.assertNotIn(name.split('.')[0].upper(), {'CON','AUX','PRN','NUL','COM1','COM¹'})
            self.assertIn('2层', name)
        self.assertEqual(suggested_pdf_filename({}, None), '单源预览_膜厚报告.pdf')

    def test_layer_table_has_one_row_per_input_and_material(self):
        process, machine, result = self.fixture(3)
        rows = _process_layer_rows(process, machine, result)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], ['1','1 · 二氧化硅','二氧化硅 SiO₂','100','0.5','监测器 2','200'])
        details = _detail_sections(result, {}, machine, process, 1, 'CPU', 'today', ['process'])
        self.assertFalse(details)  # Name already appears in the table heading; no duplicate page.
        process['steps'][0]['rate_nm_s'] = .00001
        self.assertEqual(_process_layer_rows(process, machine, result)[0][4], '1e-05')

    def test_pagination_repeats_headers_without_missing_or_duplicate_layers(self):
        process, machine, result = self.fixture(65)
        class Collector:
            def __init__(self): self.figures=[]
            def savefig(self, fig): self.figures.append(fig)
        output=Collector()
        system_fallback_only = [entry for entry in result_pdf.fontManager.ttflist
                                if entry.name == "DejaVu Sans"]
        self.assertTrue(system_fallback_only)
        with patch.object(result_pdf.fontManager, "ttflist", system_fallback_only):
            font = _font()
            self.assertEqual(font.get_family()[0], "Noto Sans CJK SC")
            bundled = result_pdf._BUNDLED_CJK_FONT.resolve()
            self.assertTrue(any(Path(entry.fname).resolve() == bundled
                                for entry in result_pdf.fontManager.ttflist))
            next_page = _process_pages(output, process, machine, result, font, start_page=2)
            self.assertGreater(len(output.figures), 1)
            self.assertEqual(next_page, 2+len(output.figures))
            numbers=[]
            for fig in output.figures:
                texts=[item.get_text() for item in fig.texts]
                self.assertIn('目标 / nm', texts); self.assertIn('蒸发源', texts)
                self.assertIn('材料', texts); self.assertIn('时长 / s', texts)
                numbers += [int(item.get_text()) for item in fig.texts
                            if abs(item.get_position()[0]-.066)<.0001 and item.get_text().isdigit()]
                canvas=FigureCanvasAgg(fig)
                with warnings.catch_warnings(record=True) as captured:
                    warnings.simplefilter('always'); canvas.draw()
                self.assertFalse([str(w.message) for w in captured if 'missing from font' in str(w.message)])
            self.assertEqual(numbers,list(range(1,66)))

    def test_pdf_font_keeps_preferred_installed_cjk_families_ahead_of_bundle(self):
        with patch.object(result_pdf.fontManager, "ttflist",
                          [SimpleNamespace(name="Noto Sans CJK JP")]):
            font = _font()
        self.assertEqual(font.get_family(), ["Noto Sans CJK JP", "DejaVu Sans"])
        self.assertIsNone(font.get_file())

    def test_default_report_stays_one_page_and_process_only_can_export(self):
        process, machine, result=self.fixture(3)
        with tempfile.TemporaryDirectory() as tmp:
            for sections in (None, ['process']):
                target=Path(tmp)/('default.pdf' if sections is None else 'process.pdf')
                export_result_pdf(target,result=result,settings={},machine=machine,process=process,
                                  elapsed_s=1,requested_device='CPU',sections=sections)
                self.assertTrue(target.read_bytes().startswith(b'%PDF'))
