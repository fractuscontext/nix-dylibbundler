# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

{ self }:
{
  bundleLibs =
    {
      pkgs,
      targetPackages,
      outPath,
      pname ? "nix-dylibbundler",
      quiet ? true,
      scanAll ? false,
      seedGlob ? null,
    }:
    let
      pkgArgs = builtins.concatStringsSep " " (
        builtins.map (p: "--pkgs ${pkgs.lib.getLib p}") targetPackages
      );

      bundlerBin = "${self.packages.${pkgs.system}.default}/bin/${pname}";

      quietFlag = pkgs.lib.optionalString quiet "--quiet";
      scanAllFlag = pkgs.lib.optionalString scanAll "--scan-all";
      seedGlobFlag = pkgs.lib.optionalString (seedGlob != null) ''--seed-glob "${seedGlob}"'';
    in
    ''
      echo "Running nix-dylibbundler on target packages..."
      ${bundlerBin} \
        --outpath "${outPath}" \
        --otool ${pkgs.darwin.cctools}/bin/otool \
        --install-name-tool ${pkgs.darwin.cctools}/bin/install_name_tool \
        ${pkgArgs} \
        ${seedGlobFlag} \
        ${quietFlag} \
        ${scanAllFlag}
    '';
}
