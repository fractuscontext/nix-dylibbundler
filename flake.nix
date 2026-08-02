# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

{

  inputs = {
    nixpkgs.url = "git+https://github.com/nixos/nixpkgs?shallow=1&ref=nixpkgs-unstable";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      git-hooks,
    }:
    let
      systems = [
        "x86_64-darwin"
        "aarch64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3Packages;
        in
        {
          default = python.buildPythonPackage {
            pname = "nix-dylibbundler";
            version = "0.1.0";
            pyproject = true;
            src = ./.;
            build-system = [ python.setuptools ];
            dependencies = [
              python.typer
              python.pytest
            ];

            meta = with pkgs.lib; {
              description = "";
              homepage = "https://github.com/fractuscontext/nix-dylibbundler";
              license = licenses.gpl3Only;
              mainProgram = "nix-dylibbundler";
              maintainers = [
                {
                  name = "Claire Tam";
                  email = "claire2026t@posteo.net";
                }
              ];
            };

          };
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nix-dylibbundler";
        };
      });

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              nixfmt.enable = true;

              # Python linting and formatting with Ruff
              ruff.enable = true;
              ruff-format.enable = true;

              # Markdown linting
              mdl = {
                enable = true;
                name = "Markdown Lint Check";
                pass_filenames = false;
              };

              # REUSE linting to ensure everything has SPDX annotations
              reuse = {
                enable = true;
                name = "SPDX License Check";
                entry = "${pkgs.reuse}/bin/reuse lint";
                pass_filenames = false;
              };
            };
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            shellHook = ''
              ${self.checks.${system}.pre-commit-check.shellHook}
            '';
          };
        }
      );

      # Export the library by importing the isolated attrs.nix file
      lib = import ./attrs.nix { inherit self; };
    };
}
