# Infrastructure

This directory contains infrastructure-related configuration and deployment scripts.

## Home Lab Deployment

The `home-lab-deploy` repository is **not** included in this Git repository to avoid CI/CD failures on GitHub Actions (since it's a private repository).

### Setup for Home Lab Use

To use the home-lab deployment integration locally:

```bash
# Run the setup script
./scripts/setup-homelab.sh
```

This will:
1. Check if you're on the home network (192.168.1.x)
2. Clone the `home-lab-deploy` repository into `infrastructure/home-lab-deploy/`
3. Update it if it already exists

### Why is this separate?

- **GitHub Actions**: CI/CD pipelines can't access private `home-lab-deploy` repository
- **Local Only**: This integration is only needed for home network deployments
- **Gitignored**: The `home-lab-deploy/` directory is automatically ignored by Git

### Manual Setup

If you prefer to set it up manually:

```bash
# Clone the home-lab-deploy repository
cd infrastructure
git clone https://github.com/milesburton/home-lab-deploy.git

# The directory is already gitignored, so it won't be committed
```

## Directory Structure

```
infrastructure/
├── README.md               # This file
└── home-lab-deploy/        # (gitignored) Cloned when needed for home use
```

## Note

The `home-lab-deploy` directory will **never** be committed to this repository. It remains local-only for home network deployments.
