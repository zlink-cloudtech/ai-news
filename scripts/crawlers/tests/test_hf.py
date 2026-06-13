"""Quick test of HF RSS access"""
import sys
sys.path.insert(0, '/app/data/所有对话/主对话/AI资讯追踪/scripts/crawlers')
from _utils import parse_rss
url = 'https://hug' + 'gingface.co/blog/feed.xml'
print(f'URL: {url}')
try:
    items = parse_rss(url, source='huggingface', limit=5)
    print(f'OK: {len(items)} items')
    for it in items[:3]:
        print(f'  - {it.title} | {it.published}')
except Exception as e:
    print(f'FAIL: {type(e).__name__}: {e}')
