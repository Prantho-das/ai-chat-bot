from fastapi import APIRouter, Request, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from datetime import datetime
from typing import Dict, Any

from app.database import get_db
from app.models import Lead, OutreachCampaign
from app.services.lead_service import lead_service
from app.services.log_service import log_service
from app.routers.admin import is_authenticated

router = APIRouter(prefix="/admin/outreach", tags=["Outreach Engine"])
templates = Jinja2Templates(directory="app/templates")

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def outreach_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login")

    res = await db.execute(select(Lead).order_by(desc(Lead.id)).limit(100))
    leads = res.scalars().all()

    camp_res = await db.execute(select(OutreachCampaign).order_by(desc(OutreachCampaign.id)).limit(20))
    campaigns = camp_res.scalars().all()

    return templates.TemplateResponse("outreach.html", {
        "request": request,
        "leads": leads,
        "campaigns": campaigns
    })

@router.post("/api/search")
async def api_search_leads(
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    idea = payload.get("idea", "")
    niche = payload.get("niche", "")
    area = payload.get("area", "")

    if not niche or not area:
        raise HTTPException(status_code=400, detail="Niche and Area are required")

    found_items = await lead_service.search_leads(niche, area, limit=8)
    
    # Save campaign record
    campaign = OutreachCampaign(
        title=f"{niche.title()} in {area.title()}",
        niche=niche,
        target_area=area,
        pitch_template=idea,
        leads_found=len(found_items)
    )
    db.add(campaign)

    saved_leads = []
    for item in found_items:
        # Generate initial AI pitch
        pitch = await lead_service.generate_cold_pitch(
            business_idea=idea or f"Automated AI Chatbot for {niche}",
            lead_name=item["business_name"],
            niche=niche,
            area=area
        )

        lead = Lead(
            business_name=item["business_name"],
            niche=item["niche"],
            area=item["area"],
            email=item.get("email"),
            phone=item.get("phone"),
            website=item.get("website"),
            instagram=item.get("instagram"),
            facebook=item.get("facebook"),
            rating=item.get("rating"),
            status="New Lead",
            cold_pitch=pitch
        )
        db.add(lead)
        saved_leads.append(lead)

    await db.commit()
    await log_service.log(
        db, 
        source="Outreach", 
        level="SUCCESS", 
        message=f"Discovered {len(saved_leads)} business leads for {niche} in {area}"
    )

    return {
        "success": True,
        "count": len(saved_leads),
        "message": f"Successfully found and saved {len(saved_leads)} leads!"
    }

@router.post("/api/generate-pitch")
async def api_generate_pitch(
    payload: Dict[str, Any] = Body(...)
):
    idea = payload.get("idea", "AI Automation Solution")
    lead_name = payload.get("lead_name", "Valued Client")
    niche = payload.get("niche", "Business")
    area = payload.get("area", "Dhaka")

    pitch = await lead_service.generate_cold_pitch(
        business_idea=idea,
        lead_name=lead_name,
        niche=niche,
        area=area
    )
    return {"success": True, "pitch": pitch}

@router.post("/api/send")
async def api_send_dm(
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    lead_id = payload.get("lead_id")
    custom_message = payload.get("message")
    channel = payload.get("channel", "Email/DM")

    if not lead_id:
        raise HTTPException(status_code=400, detail="lead_id is required")

    res = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = res.scalar_one_or_none()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.status = "Contacted"
    lead.last_contacted_at = datetime.utcnow()
    if custom_message:
        lead.cold_pitch = custom_message

    await db.commit()

    await log_service.log(
        db,
        source="Outreach",
        level="INFO",
        message=f"Cold DM sent to {lead.business_name} via {channel}",
        details=f"Email: {lead.email} | Instagram: {lead.instagram}"
    )

    return {
        "success": True,
        "message": f"Cold DM dispatched to {lead.business_name} via {channel}!"
    }
