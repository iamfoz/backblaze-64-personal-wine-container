# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Ubuntu 26.04 LTS ("Resolute") image, published as the `ubuntu26` tag (and
  `vX.Y.Z-ubuntu26` on releases). It ships alongside the default Ubuntu 24.04
  image as an early-access variant so problems can be found before it becomes
  the default. The project now tracks the two most recent Ubuntu LTS releases:
  the older is the default (`latest`) for stability, the newer is offered early,
  and the oldest is retired when it reaches end of support.

### Changed
- Updated the jlesage GUI base image to `v4.12.5` on both LTS variants.
- The WineHQ signing key is now stored as an armored `.asc` keyring referenced by
  an inline deb822 source, so the repository verifies under the stricter apt in
  Ubuntu 26.04 (which ignores a keyring saved with the old `.key` extension).
- CI builds both LTS variants in a matrix. The shared `latest` / `main` / version
  tags track the default (oldest supported) LTS; the newer LTS is published under
  its own `ubuntuNN` tag.

## [10.0.0] - 2026-06-05

### Changed
- Re-engineered for Backblaze 10.x, which is 64-bit only and requires Windows 10.
  - 64-bit WineHQ install (`winehq-stable`) via the modern deb822 `.sources`
    repository method, replacing the brittle `apt-key` / `add-apt-repository`
    setup that silently fell back to Ubuntu's old system Wine.
  - The Wine prefix is forced to report Windows 10 on every start (via the
    registry), fixing the installer's "unsupported operating system / Windows XP"
    error.
  - Install/run path moved to the 64-bit `C:\Program Files\Backblaze`.
  - Legacy 32-bit prefixes are detected and rebuilt as `win64` automatically.
  - The v10 MSI wrapper's WiX OS-version check rejects Wine (`GetVersionEx`
    reports Windows 8 to unmanifested processes), so installation now bypasses
    it: the installer's CAB payload is extracted, the program binaries are
    copied into place, and Backblaze's native `bzdoinstall.exe` is run directly
    (its only OS gate rejects server editions, which a workstation prefix passes).
  - Backblaze's in-app self-update runs a .NET MSI custom action
    (`CheckVersions`) inside `rundll32.exe`, which the Windows 8.1+ "version
    lie" reports as Windows 8 (6.2) to unmanifested processes regardless of the
    registry, aborting the update with "unsupported OS" / `MajorVerTooOld`. The
    container now writes an external `rundll32.exe.manifest` declaring a Windows
    10/11 `supportedOS` into `system32` and `syswow64` and enables
    `PreferExternalManifest`, so `GetVersionEx` reports the real Windows 10 and
    self-updates no longer break on the OS gate (#5).
- Base image moved to Ubuntu 24.04 LTS (`jlesage/baseimage-gui:ubuntu-24.04-v4`),
  with WineHQ packages installed from the `noble` repository, for a longer
  security-support window and an up-to-date userspace.
- CI builds only the `ubuntu24` image; the older `ubuntu22`, `ubuntu20`, and
  `ubuntu18` variants are no longer published.
- Removed the dead "pinned version" update path (its archive.org URL 404s and
  it was already disabled); `FORCE_LATEST_UPDATE=false` now simply keeps the
  installed client and skips the update check.
- Added a Community Applications profile (`ca_profile.xml`) and a `<TemplateURL>`
  for the Unraid CA submission.

## 1.11

### Changed
- It seems that Backblaze has disabled our source of the known-good Backblaze installer on archive.org
  Currently, all new installs will get the latest Backblaze version installed
  Also, the autoupdate functionality is now disabled by default because of this change.

## 1.10

### Changed
- Update known-good Backblaze version to 9.0.1.777
- Ubuntu 22 is now the default versioned image

## 1.9

### Changed
- Try to prevent forced Backblaze client updates

## 1.8.1

### Changed
- Optimize Dockerfiles to reduce layer count

## 1.8 - 2024-03-15

### Changed
- Update Backblaze automatically in the background
- Make startapp log file location configurable by an env var (#129, thanks @brokeh)

## 1.7.2 - 2024-02-24

### Changed
- Update known-good Backblaze version to 9.0.1.767
- Update Backblaze in the background 
- Mark ubuntu18 tag as "End of Life" and remove ubuntu18 specific troubleshooting from readme


## 1.7.1 - 2024-02-15

### Changed
- Set lower default values for DISPLAY_WIDTH and DISPLAY_HEIGHT

## 1.7 - 2024-02-07

### Added
- Automatically create symlinks for mounts (#110, thanks @xela1)
- Enable Wine Virtual Desktop mode by default

### Changed
- Updated known-good Backblaze version to 9.0.1.763
> [!NOTE]  
> Backblaze will automatically be updated to a known-good version mentioned above, if your installed version is older.
> This download of the new version may take some time, so you will only see a black screen until the download is finished. After that, the installer appears and you can update Backblaze by clicking on "install".
- Fix error `Make sure that your X server is running and that $DISPLAY is set correctly` when running basic CLI commands like `winecfg` by adding the DISPLAY environment variable to the Dockerfiles

## 1.6 - 2024-01-22

### Added
- Added backblaze client auto-update functionality to the docker (#88, thanks @traktuner)

### Changed
- By default a known-good version of the backblaze client will now be used
  - Can be overridden by adding the environment variable "FORCE_LATEST_UPDATE=true"
- The wine version in the Dockerfiles is now pinned to get more control over stability

## 1.5 - 2023-10-13
### Changed
- Dependency updates (see #18 (comment))

## 1.4 - 2023-03-22
### Changed
- Dependency updates

## 1.3 - 2023-01-11
### Changed
- Update README.md

## 1.2 - 2022-03-21
### Changed
- Fixed automated build

## 1.1 - 2022-03-21
### Added
- Ubuntu 18 based version to broaden compatibility

## 1.0 - 2022-03-05
### Added
- First versioned release
- Automatic docker build using Github Actions
- Initial platform support for linux/arm64
- Initial platform support for linux/arm/v7
- Initial platform support for linux/arm/v6

### Changed
- Updated Dependencies

[Unreleased]: https://github.com/iamfoz/backblaze-64-personal-wine-container/compare/v10.0.0...HEAD
[10.0.0]: https://github.com/iamfoz/backblaze-64-personal-wine-container/releases/tag/v10.0.0
