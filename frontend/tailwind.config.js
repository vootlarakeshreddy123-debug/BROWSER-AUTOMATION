/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#080B11",
        surface: "#12161F",
        surface2: "#171C27",
        border: "#232935",
        text: "#E6E9EF",
        muted: "#8B93A7",
        signal: "#4F8CFF",
        success: "#3DD68C",
        danger: "#FF5C5C",
        warn: "#F2B84B",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
