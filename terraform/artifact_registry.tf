resource "google_artifact_registry_repository" "polarity" {
  location      = var.region
  repository_id = "polarity"
  description   = "Docker images for polarity-backend"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  depends_on = [google_project_service.apis["artifactregistry.googleapis.com"]]
}
