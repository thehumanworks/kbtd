{ config, pkgs, ... }:
{
  outputs.kbtd = config.languages.python.import ./. { };
  packages = [ config.outputs.kbtd ];

  cachix.pull = [ "nixpkgs-python" ];

  languages.python = {
    enable = true;
    version = "3.13";
    lsp.enable = true;
    venv.enable = true;
    uv = {
      enable = true;
      sync = {
        enable = true;
        # Keep the venv for development dependencies without shadowing the Nix CLI.
        arguments = [ "--no-install-project" ];
      };
    };
  };
}
