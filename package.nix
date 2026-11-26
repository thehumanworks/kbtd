{ lib, python313Packages }:
let
  project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
  pythonFiles = dir: lib.fileset.fileFilter (file: file.hasExt "py") dir;
  py = python313Packages;

  # Not in nixpkgs. Keep the version in step with uv.lock.
  dbos = py.buildPythonPackage rec {
    pname = "dbos";
    version = "3.7.0";
    format = "wheel";
    src = py.fetchPypi {
      inherit pname version;
      format = "wheel";
      dist = "py3";
      python = "py3";
      hash = "sha256-i0k4Cl8oEXYzjKJJcy4J4Dc9YOwGTbOpET0WH+eY00E=";
    };
    dependencies = with py; [
      click
      croniter
      greenlet
      psycopg
      python-dateutil
      pyyaml
      sqlalchemy
      typing-extensions
    ];
    # The wheel asks for psycopg[binary]; nixpkgs builds psycopg from source instead.
    pythonRemoveDeps = [ "psycopg" ];
    pythonRelaxDeps = true;
    pythonImportsCheck = [ "dbos" ];
  };
in
py.buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;
  build-system = [ py.uv-build ];
  dependencies = [ dbos ];

  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      (pythonFiles ./kbtd)
      (pythonFiles ./tests)
    ];
  };

  nativeCheckInputs = [ py.unittestCheckHook ];
  pythonImportsCheck = [ "kbtd.cli" "kbtd.tui" "kbtd.watch_worker" ];

  meta = {
    inherit (project) description;
    mainProgram = "kbtd";
  };
}
