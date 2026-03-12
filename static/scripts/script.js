<script>
    document.addEventListener("DOMContentLoaded", () => {
      const menuBtn = document.getElementById("menu-button");
      const mobileMenu = document.getElementById("mobile-menu");
      menuBtn.addEventListener("click", () => {
        mobileMenu.classList.toggle("hidden");
      });
    });
  </script>