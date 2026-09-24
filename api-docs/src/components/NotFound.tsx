import { Button, Link } from "zudoku/components";

export const NotFound = () => (
  <section className="flex items-center justify-center py-20">
    <div className="mx-auto max-w-lg text-center">
      <p className="mb-4 text-6xl font-extrabold text-primary">404</p>
      <h1 className="mb-4 text-3xl font-bold">Page not found</h1>
      <p className="mb-8 text-muted-foreground">This page doesn't exist, or it has moved.</p>
      <Button asChild>
        <Link to="/">Go to the docs home</Link>
      </Button>
    </div>
  </section>
);
