resource "google_cloud_run_v2_service" "backend" {
  name     = "polarity-backend"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run.email

    scaling {
      min_instance_count = 1
      max_instance_count = 2
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/polarity/backend:latest"

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      # Non-secret environment variables
      env {
        name  = "DATABASE_HOST"
        value = google_sql_database_instance.polarity.private_ip_address
      }
      env {
        name  = "DATABASE_PORT"
        value = "5432"
      }
      env {
        name  = "DATABASE_NAME"
        value = "polarity"
      }
      env {
        name  = "DATABASE_USER"
        value = "polarity"
      }
      env {
        name  = "APP_TIMEZONE"
        value = var.app_timezone
      }
      env {
        name  = "SEND_HOUR"
        value = tostring(var.send_hour)
      }
      env {
        name  = "SEND_MINUTE"
        value = tostring(var.send_minute)
      }
      env {
        name  = "APNS_BUNDLE_ID"
        value = var.apns_bundle_id
      }
      env {
        name  = "APNS_USE_SANDBOX"
        value = "false"
      }
      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      # Secrets as environment variables
      env {
        name = "DATABASE_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["polarity-db-password"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["polarity-openai-key"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "CRON_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["polarity-cron-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "APNS_KEY_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["polarity-apns-key-id"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "APNS_TEAM_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["polarity-apns-team-id"].secret_id
            version = "latest"
          }
        }
      }

      # APNs .p8 key mounted as a file (multiline secret)
      volume_mounts {
        name       = "apns-key"
        mount_path = "/secrets/apns"
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 3
        period_seconds        = 5
        failure_threshold     = 3
        timeout_seconds       = 3
      }

      liveness_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 30
        failure_threshold = 3
        timeout_seconds   = 3
      }
    }

    volumes {
      name = "apns-key"
      secret {
        secret = google_secret_manager_secret.apns_key_p8.secret_id
        items {
          version = "latest"
          path    = "apns_key.p8"
        }
      }
    }

    timeout = "300s"
  }

  depends_on = [
    google_project_service.apis["run.googleapis.com"],
    google_secret_manager_secret_iam_member.cloud_run_access,
    google_secret_manager_secret_iam_member.apns_key_p8_access,
  ]
}
