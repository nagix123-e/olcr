/** Prevent late async results from an old UI selection overwriting current state. */
export class RequestFence {
  private generation = 0;

  begin(): number {
    return ++this.generation;
  }

  invalidate(): void {
    this.generation++;
  }

  snapshot(): number {
    return this.generation;
  }

  isCurrent(generation: number): boolean {
    return generation === this.generation;
  }
}
