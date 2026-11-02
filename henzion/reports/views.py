from datetime import date, timedelta

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import CompanySettings
from core.permissions import owner_required

from .models import ReportLog
from django.views.decorators.http import require_POST

from .services import build_report, report_title, send_eod_report, send_report


def _range_from_request(request):
    kind = request.GET.get("kind", "daily")
    today = timezone.localdate()
    try:
        picked = date.fromisoformat(request.GET["date"]) if request.GET.get("date") else today
    except ValueError:
        picked = today
    if kind == "weekly":
        start = picked - timedelta(days=picked.weekday())
        end = start + timedelta(days=6)
    elif kind == "monthly":
        start = picked.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        end = nxt - timedelta(days=1)
    else:
        kind, start, end = "daily", picked, picked
    return kind, picked, start, end


@owner_required
def report_view(request):
    kind, picked, start, end = _range_from_request(request)
    if request.method == "POST":
        ok, message = send_report(kind, start, end, force=True)
        (messages.success if ok else messages.error)(request, message)
        return redirect(f"{reverse('reports')}?kind={kind}&date={picked.isoformat()}")
    return render(request, "reports/report.html", {
        "r": build_report(start, end), "kind": kind, "picked": picked,
        "title": report_title(kind, start, end),
        "boss_email": CompanySettings.get().boss_email,
        "history": ReportLog.objects.all()[:8],
    })


@owner_required
@require_POST
def eod_send(request):
    """The dashboard button: email today's sales report to the boss now."""
    ok, message = send_eod_report()
    (messages.success if ok else messages.error)(request, "Today's report was emailed to you." if ok else message)
    return redirect("dashboard")
