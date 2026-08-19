import { AuthGuard } from "@/components/auth/auth-guard";
import { MobileBottomNav } from "@/components/layout/mobile-nav";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";

export default function AppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <AuthGuard>
    <div className="flex min-h-dvh">
      <Sidebar />

      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />

        {/* pb-20 clears the mobile bottom bar; it collapses on lg where the
            bar is not rendered. */}
        <main
          id="main-content"
          className="flex-1 px-4 py-6 pb-24 sm:px-6 lg:px-8 lg:pb-8"
        >
          <div className="mx-auto w-full max-w-[100rem]">{children}</div>
        </main>
      </div>

      <MobileBottomNav />
    </div>
    </AuthGuard>
  );
}
