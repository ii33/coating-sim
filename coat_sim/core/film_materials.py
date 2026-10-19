"""Independent film/substrate use over the existing material library.

Built-in optical references are read-only, conditional data. Their k remains
unknown. A user's explicit lossless assumption is required to compute with them.
"""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import hashlib
import stat

from .atomic_io import replace_with_retry
from .material_business import MaterialLibrary
from .materials import substrate_bindable, material_library_lock
from .optical_constants import evaluate_optical_set
from .optical_substrate_presets import builtin_optical_substrates

# Preserve this current-code import seam while all catalogs share one provider.
_builtin_substrates = builtin_optical_substrates


class FilmMaterialLibrary:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.library=MaterialLibrary(self.root)
        self.usage_path=self.root/'data'/'film_design'/'material_usage.json'
        self._catalog_fingerprint = None
        self._catalog_records = None

    def _records(self, material_ids=None):
        """Reuse validated catalog contents only while every JSON byte is unchanged."""
        self.library._check_directory()
        with material_library_lock(self.library.directory):
            fingerprint = []
            for directory in (self.library.directory, self.library.override_directory):
                for path in sorted(directory.glob('*.json')):
                    info = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('material record must be a regular non-reparse file')
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for block in iter(lambda: stream.read(65536), b''):
                            digest.update(block)
                    fingerprint.append((directory.name, path.name, digest.digest()))
            fingerprint = tuple(fingerprint)
            if self._catalog_records is None or fingerprint != self._catalog_fingerprint:
                records = self._load_records()
                self._catalog_records = records
                self._catalog_fingerprint = fingerprint
            records = self._catalog_records
            if material_ids is not None:
                records = [row for row in records if row['id'] in material_ids]
            return deepcopy(records)

    def _load_records(self):
        self.library._check_directory()
        catalog = self.library.catalog_records()
        builtins = [entry['record'] for entry in catalog if entry['builtin']]
        custom = [entry['record'] for entry in catalog if not entry['builtin']]
        reserved = {row['id'].casefold() for row in builtins}
        if any(row['id'].casefold() in reserved or row['id'].lower().startswith('builtin_') for row in custom):
            raise ValueError('\u7528\u6237\u6750\u6599ID\u4e0d\u80fd\u8986\u76d6\u53ea\u8bfb\u53c2\u8003\u8bb0\u5f55')
        if len({row['id'].casefold() for row in custom}) != len(custom):
            raise ValueError('\u7528\u6237\u6750\u6599ID\u5927\u5c0f\u5199\u51b2\u7a81')

        records = []
        for entry in catalog:
            row = deepcopy(entry['record'])
            research = row.get('research_data') or {}
            row['optical_sets'] = deepcopy(row.get('optical_constants', []))
            row['readonly'] = bool(entry['readonly'])
            row['overridden'] = bool(entry['overridden'])
            row['builtin'] = bool(entry['builtin'])
            row['design_ready'] = research.get('design_ready', row.get('design_ready'))
            row['notes'] = row.get('description', '')
            records.append(row)

        return records

    def _usage(self):
        for path in (self.usage_path.parent.parent,self.usage_path.parent,self.usage_path):
            if path.exists() and (path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400):
                raise ValueError('用途注册表不接受链接或reparse point')
        if not self.usage_path.exists():
            return {}
        if self.usage_path.stat().st_size>1000000:
            raise ValueError('用途注册表超过大小上限')
        value=json.loads(self.usage_path.read_text(encoding='utf-8'))
        if not isinstance(value,dict) or set(value)!={'schema_version','usage'} or value['schema_version']!=1 or not isinstance(value['usage'],dict):
            raise ValueError('用途注册表版本/结构无效')
        for identifier,purposes in value['usage'].items():
            if not isinstance(identifier,str) or not isinstance(purposes,dict) or set(purposes)-{'film','substrate','medium'} or any(type(x) is not bool for x in purposes.values()):
                raise ValueError('用途注册表记录无效')
        return value['usage']

    def options(self,purpose='film'):
        if purpose not in ('film','substrate','medium'):
            raise ValueError('材料用途须为 film/substrate/medium')
        usage=self._usage()
        result=[]
        for row in self._records():
            roles=row.get('roles')
            if purpose=='substrate':
                # Preserve the historical substrate binder, including medium roles.
                default=substrate_bindable(row)
            elif purpose=='medium':
                default='medium' in roles if roles is not None else False
            else:
                default='film' in roles if roles is not None else row.get('category', 'film') == 'film'
            if usage.get(row['id'],{}).get(purpose,default):
                result.append(row)
        return result

    def options_many(self,purposes):
        """Filter multiple picker roles from one validated catalog snapshot."""
        purposes=tuple(dict.fromkeys(purposes))
        if any(purpose not in ('film','substrate','medium') for purpose in purposes):
            raise ValueError('������;��Ϊ film/substrate/medium')
        if not purposes:
            return {}
        usage=self._usage()
        records=self._records()
        result={purpose:[] for purpose in purposes}
        for row in records:
            roles=row.get('roles')
            material_usage=usage.get(row['id'],{})
            for purpose in purposes:
                if purpose=='substrate':
                    # Preserve the historical substrate binder, including medium roles.
                    default=substrate_bindable(row)
                elif purpose=='medium':
                    default='medium' in roles if roles is not None else False
                else:
                    default='film' in roles if roles is not None else row.get('category', 'film') == 'film'
                if material_usage.get(purpose,default):
                    result[purpose].append(row)
        return result

    def get(self,material_id):
        for row in self._records():
            if row['id']==material_id:
                return row
        raise ValueError('找不到材料: '+str(material_id))

    def set_usage(self,material_id,purpose,enabled=True):
        if purpose not in ('film','substrate','medium') or type(enabled) is not bool:
            raise ValueError('用途须为 film/substrate/medium，enabled为bool')
        self.get(material_id)
        self._usage()
        with material_library_lock(self.usage_path.parent):
            usage=self._usage()
            usage.setdefault(material_id,{})[purpose]=enabled
            value={'schema_version':1,'usage':usage}
            descriptor,temp=tempfile.mkstemp(prefix='.material-usage-',suffix='.json',dir=self.usage_path.parent)
            try:
                with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                    json.dump(value,stream,ensure_ascii=False,allow_nan=False,indent=2)
                    stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
                replace_with_retry(temp,self.usage_path)
            finally:
                if os.path.exists(temp): os.unlink(temp)
        return deepcopy(value)

    def evaluate(self,material_id,optical_set_id,wavelength_nm,assume_lossless=False):
        if type(assume_lossless) is not bool:
            raise ValueError('无损假设须明确bool')
        material=self.get(material_id)
        record=next((r for r in material['optical_sets'] if r['id']==optical_set_id),None)
        if record is None:
            raise ValueError('找不到光学常数组: '+str(optical_set_id))
        n,k=evaluate_optical_set(record,wavelength_nm,'nm')
        if k is None:
            if not assume_lossless:
                raise ValueError('k未知；须显式选择无损假设后计算')
            k=0.0
        return complex(n,k)
