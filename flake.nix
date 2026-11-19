{
  description = "KBTD — Markdown Kanban in the terminal";

  inputs.nixpkgs.url = "github:cachix/devenv-nixpkgs/rolling";

  outputs = { nixpkgs, ... }: {
    packages = nixpkgs.lib.mapAttrs (_system: pkgs:
      let
        kbtd = pkgs.callPackage ./package.nix { };
      in
      { inherit kbtd; default = kbtd; }
    ) nixpkgs.legacyPackages;
  };
}
