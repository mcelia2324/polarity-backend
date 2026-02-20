resource "random_password" "cron_secret" {
  length  = 32
  special = false
}

locals {
  secrets = {
    "polarity-db-password"  = var.db_password
    "polarity-openai-key"   = var.openai_api_key
    "polarity-cron-secret"  = random_password.cron_secret.result
    "polarity-apns-key-id"  = var.apns_key_id != "" ? var.apns_key_id : "PLACEHOLDER"
    "polarity-apns-team-id" = var.apns_team_id != "" ? var.apns_team_id : "PLACEHOLDER"
  }
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = local.secrets
  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "secrets" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret_iam_member" "cloud_run_access" {
  for_each  = local.secrets
  secret_id = google_secret_manager_secret.secrets[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}

# APNs .p8 key stored separately (mounted as volume for multiline support)
resource "google_secret_manager_secret" "apns_key_p8" {
  secret_id = "polarity-apns-key-p8"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis["secretmanager.googleapis.com"]]
}

resource "google_secret_manager_secret_version" "apns_key_p8" {
  secret      = google_secret_manager_secret.apns_key_p8.id
  secret_data = var.apns_key_p8 != "" ? var.apns_key_p8 : "PLACEHOLDER"
}

resource "google_secret_manager_secret_iam_member" "apns_key_p8_access" {
  secret_id = google_secret_manager_secret.apns_key_p8.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}
