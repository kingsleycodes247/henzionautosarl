from django.db import models


class ReportLog(models.Model):
    """One row per report sent, so a retry or a second cron run never emails it twice."""

    DAILY, WEEKLY, MONTHLY, EOD = "daily", "weekly", "monthly", "eod"
    KIND_CHOICES = [(DAILY, "Daily"), (WEEKLY, "Weekly"), (MONTHLY, "Monthly"), (EOD, "End of day")]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    period_start = models.DateField()
    period_end = models.DateField()
    recipient = models.CharField(max_length=200, blank=True)
    ok = models.BooleanField(default=False)
    sent_at = models.DateTimeField(auto_now_add=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.kind} {self.period_start} ({'sent' if self.ok else 'failed'})"
