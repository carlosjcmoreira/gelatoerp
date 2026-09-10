"""Rendering of the persisted, immutable customer proposal snapshot."""
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def proposal_view_model(event):
    """Pure normalization shared by HTML/PDF tests and renderers."""
    return {
        "customer": event.get("company_name") or event.get("client_name") or "",
        "customer_type": event.get("customer_type") or "particular",
        "event_name": event.get("event_name") or "Proposta de evento",
        "event_type": event.get("event_type") or "",
        "occurrences": event.get("occurrences_public") or [],
        "flavours": event.get("flavours") or [],
        "resources": event.get("resource_requirements_snapshot") or {},
        "assumptions": event.get("logistics_message") or "",
        "items": event.get("quote_items_public") or [],
        "totals": event.get("quote_totals") or {},
        "revision": event.get("quote_revision") or "",
        "created_at": event.get("quote_created_at"),
    }


def render_quote_pdf(event, brand=None):
    model = proposal_view_model(event)
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    y = height - 48

    def line(text="", bold=False):
        nonlocal y
        if y < 55:
            pdf.showPage(); y = height - 48
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        # Keep generated PDFs readable without leaking unbounded user text.
        words = str(text).split()
        current = ""
        for word in words:
            if len(current) + len(word) > 105:
                pdf.drawString(45, y, current); y -= 14; current = ""
            current += (" " if current else "") + word
        if current or not text:
            pdf.drawString(45, y, current); y -= 14

    pdf.setTitle(model["event_name"])
    line((brand or {}).get("brand_name", "Scoopy"), True)
    line(model["event_name"], True)
    line(f"Cliente: {model['customer']} · Tipo: {model['customer_type']}")
    line(f"Evento: {model['event_type']}")
    line("Datas e locais", True)
    for occurrence in model["occurrences"]:
        line(f"{occurrence.get('event_date') or 'A confirmar'} · {occurrence.get('venue') or ''} · {occurrence.get('venue_address') or ''}")
    line("Sabores", True)
    for flavour in model["flavours"]:
        line(f"{flavour.get('name', flavour.get('sabor', ''))} · {flavour.get('kg', '')} kg")
    line("Recursos e requisitos", True)
    for code, resource in model["resources"].items():
        line(f"{code}: {resource}")
    if model["assumptions"]:
        line("Pressupostos", True); line(model["assumptions"])
    if model["customer_type"] == "empresa":
        line("A nossa equipa irá contactar pessoalmente para preparar uma proposta.", True)
    else:
        line("Linhas da proposta", True)
        for item in model["items"]:
            line(f"{item.get('descricao', '')} × {item.get('quantidade', '')}: {item.get('total_gross', 0)} EUR")
        totals = model["totals"]
        line(f"Sem IVA: {totals.get('total_net', 'A confirmar')} EUR")
        line(f"IVA: {totals.get('total_vat', 'A confirmar')} EUR")
        line(f"Total: {totals.get('total_gross', 0)} EUR", True)
        line(f"Revisão: {model['revision']} · Data: {model['created_at'] or ''}")
    pdf.save()
    return out.getvalue()