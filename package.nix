{ lib, python313Packages }:
let
  project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
  pythonFiles = dir: lib.fileset.fileFilter (file: file.hasExt "py") dir;
in
python313Packages.buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;
  build-system = [ python313Packages.uv-build ];

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      (pythonFiles ./kbtd)
      (pythonFiles ./tests)
    ];
  };

  nativeCheckInputs = [ python313Packages.unittestCheckHook ];
  pythonImportsCheck = [ "kbtd.cli" "kbtd.tui" ];

  meta = {
    inherit (project) description;
    mainProgram = "kbtd";
  };
}
