terraform {
  backend "http" {
    address        = "http://localhost:6061/?type=git&repository=https://github.com/pvginkel/TerraformState&ref=main&state=scratch/terraform.tfstate"
    lock_address   = "http://localhost:6061/?type=git&repository=https://github.com/pvginkel/TerraformState&ref=main&state=scratch/terraform.tfstate"
    unlock_address = "http://localhost:6061/?type=git&repository=https://github.com/pvginkel/TerraformState&ref=main&state=scratch/terraform.tfstate"
  }
}
