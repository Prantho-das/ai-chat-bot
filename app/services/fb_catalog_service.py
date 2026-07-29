import json
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import KnowledgeEntry
from app.helpers import get_bot_setting

class FBCatalogService:
    async def get_catalog_feed_xml(self, db: AsyncSession, host_url: str) -> str:
        """Generates RSS XML Feed for Meta Commerce Manager / Facebook Catalog Import"""
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)
        res = await db.execute(stmt)
        entries = res.scalars().all()

        xml = ['<?xml version="1.0" encoding="UTF-8"?>']
        xml.append('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">')
        xml.append('  <channel>')
        xml.append('    <title>MarketFlow Product Catalog Feed</title>')
        xml.append(f'    <link>{host_url}</link>')
        xml.append('    <description>Auto-generated Product Catalog Feed from Knowledge Base</description>')

        for entry in entries:
            # Extract basic title, description, and dummy price if found
            item_id = f"KB-{entry.id}"
            title = entry.title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            description = entry.content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:500]
            
            xml.append('    <item>')
            xml.append(f'      <g:id>{item_id}</g:id>')
            xml.append(f'      <g:title>{title}</g:title>')
            xml.append(f'      <g:description>{description}</g:description>')
            xml.append(f'      <g:link>{host_url}/admin/knowledge</g:link>')
            xml.append('      <g:availability>in stock</g:availability>')
            xml.append('      <g:condition>new</g:condition>')
            xml.append('      <g:price>100 BDT</g:price>')
            xml.append('    </item>')

        xml.append('  </channel>')
        xml.append('</rss>')
        return "\n".join(xml)

    async def sync_catalog_to_meta(self, db: AsyncSession) -> dict:
        """Directly sync items to Facebook Commerce Catalog via Graph API Batch Endpoint"""
        catalog_id = await get_bot_setting(db, "fb_catalog_id", "")
        access_token = await get_bot_setting(db, "fb_page_access_token", "")

        if not catalog_id or not access_token:
            return {"success": False, "error": "Catalog ID or Page Access Token is missing in Bot Settings"}

        stmt = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)
        res = await db.execute(stmt)
        entries = res.scalars().all()

        requests = []
        for entry in entries:
            requests.append({
                "method": "UPDATE",
                "data": {
                    "id": f"KB-{entry.id}",
                    "title": entry.title,
                    "description": entry.content[:500],
                    "availability": "in stock",
                    "condition": "new",
                    "price": "100 BDT",
                    "link": "https://facebook.com"
                }
            })

        url = f"https://graph.facebook.com/v19.0/{catalog_id}/items_batch"
        payload = {
            "access_token": access_token,
            "item_type": "PRODUCT_ITEM",
            "requests": json.dumps(requests)
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, data=payload, timeout=15)
                res_data = resp.json()
                if resp.status_code == 200:
                    return {"success": True, "data": res_data, "synced_count": len(entries)}
                else:
                    return {"success": False, "error": res_data.get("error", {}).get("message", "Sync failed")}
            except Exception as e:
                return {"success": False, "error": str(e)}

fb_catalog_service = FBCatalogService()
