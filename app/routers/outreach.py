from fastapi import APIRouter, Request, Depends, HTTPException, Body, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from datetime import datetime
from typing import Dict, Any
import csv
import io

from app.database import get_db
from app.models import Lead, OutreachCampaign, EmailTemplate
from app.services.lead_service import lead_service
from app.services.log_service import log_service
from app.routers.admin import is_authenticated, render_admin_page, get_current_user_context

router = APIRouter(prefix="/admin/outreach", tags=["Outreach Engine"])

def _get_target_company_id(request: Request) -> int | None:
    user_ctx = get_current_user_context(request) or {}
    active_comp_id = request.cookies.get("active_company_id", "all")
    if user_ctx.get("role") == "company_user" and user_ctx.get("company_id"):
        return user_ctx.get("company_id")
    elif active_comp_id and active_comp_id != "all":
        try:
            return int(active_comp_id)
        except ValueError:
            return None
    return None

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def outreach_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not is_authenticated(request):
        return RedirectResponse(url="/admin/login")

    target_comp_id = _get_target_company_id(request)

    stmt_lead = select(Lead)
    if target_comp_id:
        stmt_lead = stmt_lead.where(Lead.company_id == target_comp_id)
    stmt_lead = stmt_lead.order_by(desc(Lead.id)).limit(100)
    res = await db.execute(stmt_lead)
    leads = res.scalars().all()

    stmt_camp = select(OutreachCampaign)
    if target_comp_id:
        stmt_camp = stmt_camp.where(OutreachCampaign.company_id == target_comp_id)
    stmt_camp = stmt_camp.order_by(desc(OutreachCampaign.id)).limit(20)
    camp_res = await db.execute(stmt_camp)
    campaigns = camp_res.scalars().all()

    stmt_temp = select(EmailTemplate)
    if target_comp_id:
        stmt_temp = stmt_temp.where(EmailTemplate.company_id == target_comp_id)
    stmt_temp = stmt_temp.order_by(desc(EmailTemplate.id))
    temp_res = await db.execute(stmt_temp)
    templates_list = temp_res.scalars().all()

    return await render_admin_page("outreach.html", request, db, {
        "leads": leads,
        "campaigns": campaigns,
        "templates": templates_list,
        "target_company_id": target_comp_id
    })

@router.post("/api/search")
async def api_search_leads(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    target_comp_id = _get_target_company_id(request) or 1
    idea = payload.get("idea", "")
    niche = payload.get("niche", "")
    area = payload.get("area", "")
    custom_query = payload.get("custom_query", "")

    if not custom_query and (not niche or not area):
        raise HTTPException(status_code=400, detail="Niche/Area or Custom Query is required")

    found_items = await lead_service.search_leads(niche, area, limit=8, custom_query=custom_query)
    
    title_val = custom_query if custom_query else f"{niche.title()} in {area.title()}"
    campaign = OutreachCampaign(
        company_id=target_comp_id,
        title=title_val,
        niche=niche or "custom",
        target_area=area or "custom",
        pitch_template=idea,
        leads_found=len(found_items)
    )
    db.add(campaign)

    saved_leads = []
    for item in found_items:
        pitch = await lead_service.generate_cold_pitch(
            business_idea=idea or f"Automated AI Chatbot Solution",
            lead_name=item["business_name"],
            niche=item["niche"],
            area=item["area"]
        )

        lead = Lead(
            company_id=target_comp_id,
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
        "SUCCESS", 
        "Outreach", 
        f"Discovered {len(saved_leads)} business leads for query: {title_val} (Company #{target_comp_id})"
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

@router.post("/api/send-bulk")
async def api_send_bulk_dm(
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    lead_ids = payload.get("lead_ids", [])
    channel = payload.get("channel", "Email/DM")
    custom_message = payload.get("message")

    if not lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids are required")

    res = await db.execute(select(Lead).where(Lead.id.in_(lead_ids)))
    leads = res.scalars().all()

    sent_count = 0
    for lead in leads:
        lead.status = "Contacted"
        lead.last_contacted_at = datetime.utcnow()
        if custom_message:
            # We can personalize it dynamically by replacing {business_name}
            personal_msg = custom_message.replace("{business_name}", lead.business_name or "Client")
            lead.cold_pitch = personal_msg

        sent_count += 1
        await log_service.log(
            "INFO",
            "Outreach",
            f"Bulk Cold DM sent to {lead.business_name} via {channel}",
            details=f"Email: {lead.email} | Instagram: {lead.instagram}"
        )

    await db.commit()
    return {
        "success": True,
        "count": sent_count,
        "message": f"Successfully sent bulk Cold DMs to {sent_count} leads!"
    }

@router.post("/api/upload-csv")
async def api_upload_csv(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")

    target_comp_id = _get_target_company_id(request) or 1
    contents = await file.read()
    decoded = contents.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded))

    imported_count = 0
    for row in reader:
        # Check standard headers
        business_name = row.get("business_name") or row.get("name")
        email = row.get("email")
        phone = row.get("phone")
        niche = row.get("niche") or "imported"
        area = row.get("area") or "imported"
        website = row.get("website")
        instagram = row.get("instagram")
        facebook = row.get("facebook")

        if not email and not phone:
            continue

        lead = Lead(
            company_id=target_comp_id,
            business_name=business_name or f"Imported Lead {imported_count+1}",
            email=email,
            phone=phone,
            niche=niche,
            area=area,
            website=website,
            instagram=instagram,
            facebook=facebook,
            status="New Lead"
        )
        db.add(lead)
        imported_count += 1

    await db.commit()
    await log_service.log(
        "SUCCESS",
        "Outreach",
        f"Successfully imported {imported_count} leads via CSV upload for Company #{target_comp_id}"
    )

    return {
        "success": True,
        "count": imported_count,
        "message": f"Successfully imported {imported_count} leads!"
    }

@router.get("/api/templates")
async def api_get_templates(request: Request, db: AsyncSession = Depends(get_db)):
    target_comp_id = _get_target_company_id(request)
    stmt = select(EmailTemplate)
    if target_comp_id:
        stmt = stmt.where(EmailTemplate.company_id == target_comp_id)
    stmt = stmt.order_by(desc(EmailTemplate.id))
    res = await db.execute(stmt)
    templates_list = res.scalars().all()
    return [{"id": t.id, "name": t.name, "subject": t.subject, "body": t.body} for t in templates_list]

@router.post("/api/templates")
async def api_upsert_template(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    target_comp_id = _get_target_company_id(request) or 1
    template_id = payload.get("id")
    name = payload.get("name")
    subject = payload.get("subject", "")
    body = payload.get("body")

    if not name or not body:
        raise HTTPException(status_code=400, detail="Name and Body are required")

    if template_id:
        res = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
        template = res.scalar_one_or_none()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        template.name = name
        template.subject = subject
        template.body = body
    else:
        template = EmailTemplate(company_id=target_comp_id, name=name, subject=subject, body=body)
        db.add(template)

    await db.commit()
    return {"success": True, "template": {"id": template.id, "name": template.name, "subject": template.subject, "body": template.body}}

@router.delete("/api/templates/{template_id}")
async def api_delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
    template = res.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    await db.delete(template)
    await db.commit()
    return {"success": True, "message": "Template deleted successfully"}
