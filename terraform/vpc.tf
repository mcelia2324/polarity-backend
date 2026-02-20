resource "google_compute_global_address" "private_ip_range" {
  name          = "polarity-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = "projects/${var.project_id}/global/networks/default"

  depends_on = [google_project_service.apis["compute.googleapis.com"]]
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = "projects/${var.project_id}/global/networks/default"
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]

  depends_on = [google_project_service.apis["servicenetworking.googleapis.com"]]
}

resource "google_vpc_access_connector" "connector" {
  name          = "polarity-vpc"
  region        = var.region
  ip_cidr_range = "10.8.0.0/28"
  network       = "default"

  min_throughput = 200
  max_throughput = 300

  depends_on = [
    google_project_service.apis["vpcaccess.googleapis.com"],
    google_project_service.apis["compute.googleapis.com"],
  ]
}
