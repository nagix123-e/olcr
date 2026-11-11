"""Versioned, data-only agent archives with portable delegate references."""
import time
import uuid
import base64
import hashlib
import io
import json
from pathlib import PurePosixPath
import stat
import zipfile
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictBool, model_validator
from .custom_agents import AgentDefinition, AgentStore
from .agent_knowledge import file_bytes, validate_file, read_file

MAX_ARCHIVE_BYTES = 50_000_000
MANIFEST_NAME = '.olcr-agent.json'

class PackageAgent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: StrictStr = Field(min_length=1, max_length=128)
    definition: AgentDefinition

class AgentPackage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['olcr.agent'] = 'olcr.agent'
    version: Literal[1] = 1
    root_id: StrictStr = Field(min_length=1, max_length=128)
    agents: list[PackageAgent] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def validate_graph(self):
        rows = {item.id: item.definition for item in self.agents}
        if len(rows) != len(self.agents) or self.root_id not in rows:
            raise ValueError('AGENT_PACKAGE_INVALID_IDS')
        for key, item in rows.items():
            if any(child == key or child not in rows for child in item.subagent_ids):
                raise ValueError('AGENT_PACKAGE_MISSING_DELEGATE')
        visited, pending = set(), [self.root_id]
        while pending:
            key = pending.pop()
            if key in visited: continue
            visited.add(key); pending.extend(rows[key].subagent_ids)
        if visited != set(rows): raise ValueError('AGENT_PACKAGE_UNRELATED_AGENT')
        if len(self.model_dump_json().encode()) > 100_000_000:
            raise ValueError('AGENT_PACKAGE_TOO_LARGE')
        return self

def export_package(store: AgentStore, agent_id: str) -> AgentPackage:
    pending, visited, entries = [agent_id], set(), []
    while pending:
        key = pending.pop(0)
        if key in visited: continue
        visited.add(key)
        if len(visited) > 32: raise ValueError('AGENT_PACKAGE_TOO_MANY_DELEGATES')
        agent = store.get(key)
        if not agent or agent['archived']: raise ValueError('AGENT_PACKAGE_AGENT_UNAVAILABLE')
        definition = AgentDefinition.model_validate({name: agent[name] for name in AgentDefinition.model_fields})
        entries.append(PackageAgent(id=key, definition=definition))
        pending.extend(definition.subagent_ids)
    return AgentPackage(root_id=agent_id, agents=entries)

def import_package(store: AgentStore, package: AgentPackage):
    # Definitions are inert data. Missing models/skills stay selected and are
    # checked by existing execution policies when the user actually uses them.
    ids = {entry.id: str(uuid.uuid4()) for entry in package.agents}
    now = time.time()
    with store.db.connect() as connection:
        for entry in package.agents:
            definition = entry.definition.model_copy(update={'subagent_ids': [ids[key] for key in entry.definition.subagent_ids]})
            connection.execute('INSERT INTO custom_agents VALUES(?,?,1,0,?,?)',
                               (ids[entry.id], definition.model_dump_json(), now, now))
    return {'agent': store.get(ids[package.root_id]), 'imported_count': len(ids)}


class ArchiveKnowledge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr = Field(min_length=1, max_length=200)
    path: StrictStr = Field(min_length=1, max_length=240)
    mime_type: StrictStr
    sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    original: StrictBool

class ArchiveDefinition(AgentDefinition):
    knowledge: list[ArchiveKnowledge] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def bounded(self):
        # Validate the remaining definition with the ordinary schema; file
        # references are validated before any agent is inserted.
        AgentDefinition.model_validate({**self.model_dump(), 'knowledge': []})
        return self

class ArchiveAgent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: StrictStr = Field(min_length=1, max_length=128)
    definition: ArchiveDefinition

class ArchiveManifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['olcr.agent']
    version: Literal[2]
    root_id: StrictStr = Field(min_length=1, max_length=128)
    agents: list[ArchiveAgent] = Field(min_length=1, max_length=32)

def export_zip(store: AgentStore, agent_id: str) -> bytes:
    package = export_package(store, agent_id)
    manifest = package.model_dump(); manifest['version'] = 2
    output = io.BytesIO()
    total = 0
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for agent_index, agent in enumerate(manifest['agents']):
            references = []
            for index, item in enumerate(agent['definition']['knowledge']):
                raw = file_bytes(item)
                original = item.get('file_base64') is not None
                suffix = PurePosixPath(item['name']).suffix.lower() if original else '.txt'
                path = f'knowledge/{agent_index}/{index}{suffix}'
                mime = validate_file(item['name'], raw) if original else 'text/plain'
                total += len(raw)
                if total > MAX_ARCHIVE_BYTES: raise ValueError('AGENT_PACKAGE_TOO_LARGE')
                archive.writestr(path, raw)
                references.append({'name':item['name'], 'path':path, 'mime_type':mime,
                                   'sha256':hashlib.sha256(raw).hexdigest(), 'original':original})
            agent['definition']['knowledge'] = references
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    value = output.getvalue()
    if len(value) > MAX_ARCHIVE_BYTES: raise ValueError('AGENT_PACKAGE_TOO_LARGE')
    return value

def parse_zip(data: bytes):
    if len(data) > MAX_ARCHIVE_BYTES: raise ValueError('AGENT_PACKAGE_TOO_LARGE')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            all_names = [item.filename for item in infos]
            names = [item.filename for item in infos if not item.is_dir()]
            if len(all_names) > 800 or len(set(all_names)) != len(all_names) or MANIFEST_NAME not in names:
                raise ValueError('AGENT_PACKAGE_INVALID_STRUCTURE')
            if sum(item.file_size for item in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError('AGENT_PACKAGE_TOO_LARGE')
            for item in infos:
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                        or (item.is_dir() and not item.filename.startswith('knowledge/'))
                        or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16)
                        or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ValueError('AGENT_PACKAGE_UNSAFE_MEMBER')
            if archive.getinfo(MANIFEST_NAME).file_size > 20_000_000:
                raise ValueError('AGENT_PACKAGE_MANIFEST_TOO_LARGE')
            manifest = ArchiveManifest.model_validate_json(archive.read(MANIFEST_NAME))
            expected = {MANIFEST_NAME}
            # First validate every reference/hash/type; no text extraction or DB
            # write is performed on a partially valid archive.
            files = {}
            for agent in manifest.agents:
                for knowledge in agent.definition.knowledge:
                    if knowledge.path in expected or not knowledge.path.startswith('knowledge/') or knowledge.path not in names:
                        raise ValueError('AGENT_PACKAGE_INVALID_REFERENCE')
                    expected.add(knowledge.path)
                    raw = archive.read(knowledge.path)
                    if hashlib.sha256(raw).hexdigest() != knowledge.sha256:
                        raise ValueError('AGENT_PACKAGE_HASH_MISMATCH')
                    mime = validate_file(knowledge.name if knowledge.original else knowledge.path, raw)
                    if mime != knowledge.mime_type or (not knowledge.original and mime != 'text/plain'):
                        raise ValueError('AGENT_PACKAGE_TYPE_MISMATCH')
                    files[knowledge.path] = raw
            if set(names) != expected: raise ValueError('AGENT_PACKAGE_UNEXPECTED_FILE')
            hydrated = manifest.model_dump(); hydrated['version'] = 1
            # Validate the graph before expensive PDF/image parsing.
            skeleton = json.loads(json.dumps(hydrated))
            for agent in skeleton['agents']: agent['definition']['knowledge'] = []
            AgentPackage.model_validate(skeleton)
            for agent in hydrated['agents']:
                restored = []
                for item in agent['definition']['knowledge']:
                    raw = files[item['path']]
                    restored.append(read_file(item['name'], raw) if item['original'] else
                                    {'name':item['name'], 'content':raw.decode('utf-8')})
                agent['definition']['knowledge'] = restored
            package = AgentPackage.model_validate(hydrated)
    except (zipfile.BadZipFile, KeyError, UnicodeError, RuntimeError) as exc:
        raise ValueError('AGENT_PACKAGE_INVALID_ZIP') from exc
    return package

def import_zip(store: AgentStore, data: bytes):
    return import_package(store, parse_zip(data))
