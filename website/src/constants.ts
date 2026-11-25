export const RELEASES_URL = 'https://github.com/nagix123-e/olcr/releases/' as const;
export const PROJECT_REPOSITORY_URL = 'https://github.com/nagix123-e/olcr' as const;

export const assetPath = (filename: string) =>
  `${import.meta.env.BASE_URL}${filename}`;
