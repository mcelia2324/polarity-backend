variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "us-east1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "db_password" {
  description = "PostgreSQL user password"
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

variable "apns_key_p8" {
  description = "APNs auth key (.p8 file contents)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "apns_key_id" {
  description = "APNs Key ID"
  type        = string
  sensitive   = true
  default     = ""
}

variable "apns_team_id" {
  description = "APNs Team ID"
  type        = string
  sensitive   = true
  default     = ""
}

variable "apns_bundle_id" {
  description = "iOS app bundle identifier"
  type        = string
  default     = "mcelia.PolarityApp"
}

variable "app_timezone" {
  description = "Application timezone"
  type        = string
  default     = "America/Chicago"
}

variable "send_hour" {
  description = "Hour to send daily notification (in app_timezone)"
  type        = number
  default     = 8
}

variable "send_minute" {
  description = "Minute to send daily notification"
  type        = number
  default     = 0
}
