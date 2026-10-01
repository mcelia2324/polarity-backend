resource "google_artifact_registry_repository" "polarity" {
  location      = var.region
  repository_id = "polarity"
  description   = "Docker images for polarity-backend"
  format        = "DOCKER"

  # A KEEP policy on its own deletes nothing; it only exempts versions from DELETE policies.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis["artifactregistry.googleapis.com"]]
}
