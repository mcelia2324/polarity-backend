resource "google_sql_database_instance" "polarity" {
  name             = "polarity-${var.environment}"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_HDD"
    disk_autoresize   = false

    ip_configuration {
      ipv4_enabled    = false
      private_network = "projects/${var.project_id}/global/networks/default"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "04:00"
      point_in_time_recovery_enabled = false
      transaction_log_retention_days = 3
      backup_retention_settings {
        retained_backups = 7
      }
    }

    maintenance_window {
      day          = 7
      hour         = 6
      update_track = "stable"
    }

    database_flags {
      name  = "max_connections"
      value = "50"
    }
  }

  deletion_protection = true

  depends_on = [google_service_networking_connection.private_vpc]
}

resource "google_sql_database" "polarity" {
  name     = "polarity"
  instance = google_sql_database_instance.polarity.name
}

resource "google_sql_user" "polarity" {
  name     = "polarity"
  instance = google_sql_database_instance.polarity.name
  password = var.db_password
}
