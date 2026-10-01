resource "google_cloud_scheduler_job" "daily_cron" {
  name        = "polarity-daily-generate"
  description = "Generate daily word pair and send push notifications"
  region      = var.region
  schedule    = "0 ${var.send_hour} * * *"
  time_zone   = var.app_timezone

  retry_config {
    retry_count          = 3
    min_backoff_duration = "10s"
    max_backoff_duration = "300s"
  }

  http_target {
    uri         = "${google_cloud_run_v2_service.backend.uri}/cron/daily"
    http_method = "POST"

    headers = {
      "X-Cron-Secret" = random_password.cron_secret.result
    }

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.backend.uri
    }
  }

  depends_on = [google_project_service.apis["cloudscheduler.googleapis.com"]]
}

# Fetch word-of-day every 5 minutes so the app's first open of the day never waits on a
# cold start (~8s) or on generating the new day's pair (~15s just after midnight). Idle
# instances are free under request-based billing and Cloud Run keeps an instance warm while
# it gets traffic, so this costs $0 (vs ~$4.75/mo for min_instance_count = 1). The response
# is also cached in memory, so the app's own request is served without touching the DB.
resource "google_cloud_scheduler_job" "keep_warm" {
  name             = "polarity-keep-warm"
  description      = "Keep an instance warm and today's word pair generated and cached"
  region           = var.region
  schedule         = "*/5 * * * *"
  time_zone        = var.app_timezone
  attempt_deadline = "180s"

  retry_config {
    retry_count = 0
  }

  http_target {
    uri         = "${google_cloud_run_v2_service.backend.uri}/api/word-of-day"
    http_method = "GET"
  }

  depends_on = [google_project_service.apis["cloudscheduler.googleapis.com"]]
}
