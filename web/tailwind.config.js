/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#17202b",
        panel: "#f7f8fb",
        line: "#d8dee8"
      },
      boxShadow: {
        soft: "0 12px 35px rgba(24, 34, 48, 0.10)"
      }
    }
  },
  plugins: []
};
