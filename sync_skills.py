#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import zipfile
import io
import urllib.request
import urllib.error
from datetime import datetime, timezone

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== 配置（可根据需要修改） ====================
GITCODE_ZIP_URL = os.environ.get(
    'GITCODE_ZIP_URL',
    'https://raw.gitcode.com/huaweicloud/huaweicloud-skills/archive/refs/heads/master.zip'
)
OUTPUT_DIR = 'skills-index'          # 输出目录
# =============================================================

# 华为云服务关键词集合（用于从描述中提取服务）
HUAWEI_CLOUD_SERVICES = {
    'ecs', 'bms', 'ims', 'as', 'evs', 'obs', 'sfs', 'cbr',
    'vpc', 'eip', 'elb', 'nat', 'vpn', 'dns',
    'cce', 'cci', 'swr', 'ucs',
    'ces', 'eps',
    'iam', 'maas', 'ges',
    'functiongraph', 'fg',
    'bss', 'billing',
    'smn', 'dcs', 'dms', 'rds', 'dds', 'gaussdb',
    'cse', 'apig', 'waf',
    'csm', 'sac', 'terraform', 'cli',
    'cnnad', 'mrs', 'dli', 'dis', 'dws', 'css',
    'modelarts', 'hilens', 'ivo',
    'hss', 'waf', 'anti_ddos', 'dew', 'kms',
    'tms', 'rms', 'ram',
    'vbs', 'auto_scaling',
}

CN_CHAR_RE = re.compile(r'[\u4e00-\u9fff]')
TRIGGER_SPLIT_RE = re.compile(r'[,\n]')
SERVICE_FROM_DESCRIPTION_RE = re.compile(
    r'\b(' + '|'.join(sorted(HUAWEI_CLOUD_SERVICES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)

def download_zip(url):
    """Download zip file with browser-like headers"""
    logger.info(f'Downloading zip from: {url}')
    # 定义一个常见的浏览器请求头，让服务器认为我们是一个真实的浏览器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    logger.info(f'Downloaded {len(data)} bytes')
    return data

def parse_frontmatter(text):
    """解析 SKILL.md 开头的 YAML frontmatter"""
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    fm_text = m.group(1)
    meta = {}
    current_key = None
    current_value = None
    in_multiline = False

    for line in fm_text.split('\n'):
        if in_multiline:
            if line.strip() == '':
                if current_key:
                    meta[current_key] = current_value
                    current_key = None
                    in_multiline = False
                continue
            stripped = line.strip()
            trigger_fm_match = re.match(
                r'^(Trigger(?:\s+keywords)?|Triggered\s+by\s+keywords):\s*(.*)',
                stripped,
            )
            if current_key == 'description' and trigger_fm_match:
                trigger_part = trigger_fm_match.group(2).strip()
                if current_value:
                    current_value = current_value.rstrip()
                meta[current_key] = current_value
                meta[trigger_fm_match.group(1)] = trigger_part
                current_key = None
                in_multiline = False
                continue
            if current_value is not None:
                current_value += '\n' + line
            continue

        kv_match = re.match(r'^(\w+)\s*:\s*(.*)', line)
        if not kv_match:
            continue
        key = kv_match.group(1)
        val = kv_match.group(2).strip()

        if val == '|':
            current_key = key
            current_value = ''
            in_multiline = True
            continue

        if val.startswith('[') and val.endswith(']'):
            items = [v.strip().strip('"').strip("'") for v in val[1:-1].split(',')]
            meta[key] = [v for v in items if v]
            continue

        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        meta[key] = val

    if current_key and current_value is not None:
        meta[current_key] = current_value
    return meta

def extract_triggers_from_frontmatter(fm):
    """从 frontmatter 中提取触发词"""
    trigger_value = ''
    for key in ['Trigger', 'Trigger keywords', 'Triggered by keywords']:
        if key in fm:
            trigger_value = fm[key]
            break
    if isinstance(trigger_value, list):
        return [t.strip().strip('"').strip("'") for t in trigger_value if t.strip()]
    if not trigger_value:
        return []
    parts = TRIGGER_SPLIT_RE.split(trigger_value)
    result = []
    for p in parts:
        p = p.strip().strip('"').strip("'").strip()
        if p:
            result.append(p)
    return result

def extract_triggers_from_description(description):
    """从描述中提取触发词"""
    if not description:
        return []
    trigger_match = re.search(
        r'(?:Trigger(?:er\s+words)?|Trigger\s+keywords|Triggered\s+by\s+keywords\s+like)\s*[:.]?\s*(.*)',
        description,
        re.IGNORECASE,
    )
    if not trigger_match:
        return []
    trigger_text = trigger_match.group(1)
    parts = TRIGGER_SPLIT_RE.split(trigger_text)
    result = []
    for p in parts:
        p = p.strip().strip('"').strip("'").strip().rstrip('.')
        p = re.sub(r'^user mentions\s*"?', '', p, flags=re.IGNORECASE).strip()
        if p:
            result.append(p)
    return result

def extract_services_from_tags(tags):
    """从 tags 中提取服务名"""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [t.strip().strip('"').strip("'") for t in tags.split(',')]
    services = []
    for tag in tags:
        tag_lower = tag.strip().lower()
        if tag_lower in HUAWEI_CLOUD_SERVICES:
            services.append(tag_lower)
    return services

def extract_services_from_description(description):
    """从描述中提取服务名"""
    if not description:
        return []
    matches = SERVICE_FROM_DESCRIPTION_RE.findall(description)
    return list(dict.fromkeys(m.lower() for m in matches))

def parse_skill_md(content_bytes, zip_path):
    """解析单个 SKILL.md 文件"""
    try:
        text = content_bytes.decode('utf-8')
    except UnicodeDecodeError:
        text = content_bytes.decode('utf-8', errors='replace')

    fm = parse_frontmatter(text)
    name = fm.get('name', fm.get('id', ''))
    if not name:
        return None

    path_parts = zip_path.replace('\\', '/').split('/')
    category = ''
    path_service = ''
    for i, part in enumerate(path_parts):
        if part == 'skills' and i + 2 < len(path_parts):
            category = path_parts[i + 1] if i + 1 < len(path_parts) else ''
            path_service = path_parts[i + 2] if i + 2 < len(path_parts) else ''
            break

    description = fm.get('description', '')
    description = description.strip()

    services = extract_services_from_tags(fm.get('tags'))
    if not services:
        services = extract_services_from_description(description)
    service = services[0] if services else path_service

    triggers = extract_triggers_from_frontmatter(fm)
    if not triggers:
        triggers = extract_triggers_from_description(description)

    return {
        'name': name,
        'category': category,
        'service': service,
        'services': services,
        'description': description,
        'triggers': triggers,
        'has_description': bool(description),
    }

def generate_index_and_map(zip_bytes):
    """从 zip 内容生成 index 和 cn-en-map"""
    skills = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        all_names = zf.namelist()
        logger.info(f'Zip contains {len(all_names)} entries')

        skill_md_paths = [
            n for n in all_names
            if n.endswith('/SKILL.md') and '/skills/' in n.replace('\\', '/')
        ]
        logger.info(f'Found {len(skill_md_paths)} SKILL.md files')

        for path in skill_md_paths:
            content = zf.read(path)
            skill = parse_skill_md(content, path)
            if skill:
                skills.append(skill)

    skills.sort(key=lambda s: s['name'])

    categories = sorted(set(s['category'] for s in skills if s['category']))
    skills_with_descriptions = sum(1 for s in skills if s['has_description'])
    skills_with_triggers = sum(1 for s in skills if s['triggers'])

    index_data = {
        'repo': 'huaweicloud/huaweicloud-skills',
        'platform': 'gitcode',
        'branch': 'master',
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'total_skills': len(skills),
        'skills_with_descriptions': skills_with_descriptions,
        'skills_with_triggers': skills_with_triggers,
        'categories': categories,
        'skills': skills,
    }

    cn_en_map = {}
    for skill in skills:
        service = skill['service']
        for trigger in skill['triggers']:
            trigger = trigger.strip()
            if not trigger:
                continue
            if CN_CHAR_RE.search(trigger) and service:
                cn_en_map[trigger] = service

    return index_data, cn_en_map

def write_local_files(index_data, cn_en_map, output_dir=OUTPUT_DIR):
    """将索引文件写入本地目录"""
    os.makedirs(output_dir, exist_ok=True)
    index_path = os.path.join(output_dir, 'index.json')
    map_path = os.path.join(output_dir, 'cn-en-map.json')
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(cn_en_map, f, ensure_ascii=False, indent=2)
    logger.info(f'Written {index_path} (total skills: {len(index_data["skills"])})')
    logger.info(f'Written {map_path} (map entries: {len(cn_en_map)})')

def main():
    try:
        # 下载 zip
        zip_bytes = download_zip(GITCODE_ZIP_URL)
        # 生成索引数据
        index_data, cn_en_map = generate_index_and_map(zip_bytes)
        # 写入本地文件
        write_local_files(index_data, cn_en_map)
        # 输出统计信息（供 GitHub Actions 日志查看）
        logger.info(f'Summary: {index_data["total_skills"]} skills, '
                    f'{index_data["skills_with_triggers"]} with triggers, '
                    f'{len(cn_en_map)} map entries')
        # 输出一个简单 JSON 结果，方便调试
        print(json.dumps({
            'status': 'success',
            'total_skills': index_data['total_skills'],
            'cn_en_map_entries': len(cn_en_map),
            'generated_at': index_data['generated_at']
        }))
    except Exception as e:
        logger.exception('Sync failed')
        print(json.dumps({'status': 'error', 'message': str(e)}))
        raise

if __name__ == '__main__':
    main()
