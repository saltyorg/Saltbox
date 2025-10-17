# ARM64 Support for Saltbox

## Overview

Saltbox now supports ARM64 (aarch64) architecture in addition to x86_64 (amd64). This enables deployment on ARM-based systems such as:
- AWS Graviton instances
- Oracle Cloud ARM instances
- Raspberry Pi 4/5 (with sufficient resources)
- Other ARM64 servers running Ubuntu 22.04 or 24.04 LTS

## Changes Made

### 1. Documentation Updates
- **README.md**: Updated to reflect ARM64 support and removed the restriction stating "ARM processors are not supported."

### 2. Architecture Detection
- **roles/sanity_check/defaults/main.yml**: Added ARM64 CPU feature detection variables for ARMv8, ARMv8.1, and ARMv8.2
- **roles/sanity_check/tasks/subtasks/10_cpu.yml**: Enhanced CPU check to detect both x86_64 and ARM64 architectures and report supported feature sets

### 3. Binary Downloads
- **roles/ctop/defaults/main.yml**: Made ctop binary download architecture-aware, automatically selecting linux-amd64 or linux-arm64 based on system architecture

### 4. APT Repository Configuration
- **roles/docker/defaults/main.yml**: Updated Docker APT repository to use the correct architecture (amd64 or arm64)
- **roles/mainline/tasks/main.yml**: Updated mainline kernel PPA to support ARM64 architecture

### 5. Docker Container Platform Support
- **resources/tasks/docker/create_docker_container.yml**: Added automatic platform detection and configuration for Docker containers
  - Sets platform to `linux/amd64` for x86_64 systems
  - Sets platform to `linux/arm64` for ARM64 systems
  - Allows per-role override via `<role_name>_docker_platform` variable

## Important Notes

### Docker Image Compatibility
Most modern Docker images from popular repositories (Docker Hub, GitHub Container Registry, etc.) support multi-architecture builds. However, you should verify that the specific images you're using support ARM64:

1. Check the image documentation on Docker Hub or the project's repository
2. Look for tags that explicitly mention ARM64/aarch64 support
3. Some older or niche images may only support x86_64

### Known Limitations
- **GPU Support**: NVIDIA GPU support is primarily designed for x86_64 systems. ARM64 systems with GPUs may require additional configuration
- **Binary-only Applications**: Some third-party tools that download pre-compiled binaries may not have ARM64 versions available
- **Performance**: While most applications run well on ARM64, some may have performance differences compared to x86_64

## Testing Recommendations

When deploying Saltbox on ARM64:

1. **Start with Core Services**: Test basic services first before adding all applications
2. **Check Container Logs**: Monitor container logs for architecture-related errors
3. **Verify Image Pulls**: Ensure Docker is pulling ARM64 images (check with `docker image inspect <image_name>`)
4. **Test Incrementally**: Add roles one at a time to identify any ARM64-specific issues

## Troubleshooting

### Image Pull Failures
If you encounter errors like "no matching manifest for linux/arm64":
- The image doesn't support ARM64 architecture
- Try finding an alternative image or check if the maintainer offers ARM64 builds
- Some roles may need image repository overrides

### Performance Issues
- ARM64 CPUs may have different performance characteristics
- Consider adjusting resource limits and worker counts for your specific hardware
- Monitor CPU and memory usage during initial setup

### Binary Download Failures
If binary downloads fail:
- Check if the upstream project provides ARM64 releases
- Some tools may need manual compilation or alternative sources
- Report issues to the Saltbox project for potential workarounds

## Architecture-Specific Variables

The following architecture mappings are used throughout Saltbox:

```yaml
# Architecture mapping
x86_64 -> amd64 (APT) -> linux/amd64 (Docker)
aarch64 -> arm64 (APT) -> linux/arm64 (Docker)
```

## Contributing

If you encounter issues with ARM64 support:
1. Check if the issue is specific to a particular role or application
2. Report issues on the Saltbox GitHub repository with:
   - Your ARM64 system specifications
   - The specific role or application causing issues
   - Any error messages or logs
3. Pull requests to improve ARM64 support are welcome!

## Future Enhancements

Potential areas for improvement:
- Additional ARM64-specific optimizations
- Enhanced GPU support for ARM64 systems
- Better detection and handling of ARM64-incompatible images
- ARM64-specific performance tuning guides
