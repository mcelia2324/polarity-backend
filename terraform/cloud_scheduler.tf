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
