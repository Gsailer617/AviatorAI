# To learn more about how to use Nix to configure your environment
# see: https://developers.google.com/idx/guides/customize-idx-env
{ pkgs, ... }: {
  # Which nixpkgs channel to use.
  channel = "stable-24.05"; # or "unstable"
  # Use https://search.nixos.org/packages to find packages
  packages = [
    pkgs.nodejs_20
    pkgs.openjdk17-bootstrap
    pkgs.util-linux
    pkgs.flutter  # Added Flutter
    pkgs.python3  # Added Python 3
    # pkgs.go
  ];
  # Sets environment variables in the workspace
  env = {
    #TODO Get a API key from https://g.co/ai/idxGetGeminiKey
    GOOGLE_GENAI_API_KEY = "AIzaSyCHdC3hYqGKDgSAzTeIvEP5oE4Fbv4P_Ko"; # Key added by user
  };
  idx = {
    # Search for the extensions you want on https://open-vsx.org/ and use "publisher.id"
    extensions = [
      # "vscodevim.vim"
      # "golang.go"
    ];

    # Workspace lifecycle hooks
    workspace = {
      # Runs when a workspace is first created
      onCreate = {
        npm-install = "npm ci --no-audit --prefer-offline --no-progress --timing";
        default.openFiles = [ "README.md" "index.ts" ];
      };
      # Runs when the workspace is (re)started
      onStart = {
        # Rely on the top-level 'env' block to set GOOGLE_GENAI_API_KEY
        run-server = ''
          npm run genkit:dev
        ''; 
      };
    };
  };
}
