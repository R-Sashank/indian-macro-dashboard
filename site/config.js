// Where the site loads its data from.
// Locally it reads the bundled site/data folder. When deployed (e.g. on
// Netlify) it reads the always-fresh copy that the scheduled job commits to
// GitHub, so a data refresh never needs a redeploy. If that is unreachable it
// falls back to the data bundled with the deploy.
window.DATA_BASE = ['localhost', '127.0.0.1'].includes(location.hostname)
  ? ''
  : 'https://raw.githubusercontent.com/R-Sashank/indian-macro-dashboard/main/site/data/';
