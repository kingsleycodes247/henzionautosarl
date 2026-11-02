/** @type {import('tailwindcss').Config} */
module.exports = {
  // Every place a Tailwind class can appear. Python files are included on purpose:
  // form widgets (core/forms.py) and template filters (core/templatetags/ui.py) add classes too,
  // and so does the cart in static/js/pos.js. A class not found here is missing from the build.
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
    "./core/**/*.py",
    "./inventory/**/*.py",
    "./sales/**/*.py",
    "./debts/**/*.py",
    "./reports/**/*.py",
  ],
  theme: {
    extend: {
      colors: { brand: { pink: "#f6097f", navy: "#1e293b", cyan: "#22d3ee" } },
    },
  },
  plugins: [],
};
