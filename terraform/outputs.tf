output "app_name" {
  value = juju_application.seaweedfs.name
}

output "endpoints" {
  value = {
    # Provides
    s3_credentials = "s3-credentials"
  }
}
